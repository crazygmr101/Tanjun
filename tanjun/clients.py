# -*- coding: utf-8 -*-
# cython: language_level=3
# BSD 3-Clause License
#
# Copyright (c) 2020-2021, Faster Speeding
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
from __future__ import annotations

__all__: list[str] = ["as_loader", "Client", "LoadableSig", "MessageAcceptsEnum", "PrefixGetterSig", "PrefixGetterSigT"]

import asyncio
import enum
import functools
import importlib
import importlib.abc as importlib_abc
import importlib.util as importlib_util
import inspect
import itertools
import logging
import typing
from collections import abc as collections

import hikari
from hikari import traits as hikari_traits
from yuyo import backoff

from . import context
from . import errors
from . import injecting
from . import traits as tanjun_traits
from . import utilities

if typing.TYPE_CHECKING:
    import pathlib
    import types

    from hikari.api import event_manager as event_manager_api

    _ClientT = typing.TypeVar("_ClientT", bound="Client")

LoadableSig = collections.Callable[["Client"], None]
"""Type hint of the callback used to load resources into a Tanjun client.

This should take one positional argument of type `Client` and return nothing.
This will be expected to initiate and resources like components to the client
through the use of it's protocol methods.
"""

PrefixGetterSig = collections.Callable[..., collections.Awaitable[collections.Iterable[str]]]
"""Type hint of a callable used to get the prefix(es) for a specific guild.

This should be an asynchronous callable which returns an iterable of strings.

!!! note
    While dependency injection is supported for this, the first positional
    argument will always be a `tanjun.traits.MessageContext`.
"""

PrefixGetterSigT = typing.TypeVar("PrefixGetterSigT", bound="PrefixGetterSig")

_LOGGER: typing.Final[logging.Logger] = logging.getLogger("hikari.tanjun.clients")


class _LoadableDescriptor:  # Slots mess with functools.update_wrapper
    def __init__(self, callback: LoadableSig, /) -> None:
        self._callback = callback
        functools.update_wrapper(self, callback)

    def __call__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        self._callback(*args, **kwargs)


def as_loader(callback: LoadableSig, /) -> LoadableSig:
    """Mark a callback as being used to load Tanjun utilities from a module.

    Parameters
    ----------
    callback : LoadableSig
        The callback used to load Tanjun utilities from a module. This
        should take one argument of type `tanjun.traits.Client`, return nothing
        and will be expected to initiate and add utilities such as components
        to the provided client using it's abstract methods.

    Returns
    -------
    LoadableSig
        The decorated load callback.
    """
    return _LoadableDescriptor(callback)


class MessageAcceptsEnum(str, enum.Enum):
    """The possible configurations for which events `Client` should execute commands based on."""

    ALL = "ALL"
    """Set the client to execute commands based on both DM and guild message create events."""

    DM_ONLY = "DM_ONLY"
    """Set the client to execute commands based only DM message create events."""

    GUILD_ONLY = "GUILD_ONLY"
    """Set the client to execute commands based only guild message create events."""

    NONE = "NONE"
    """Set the client to not execute commands based on message create events."""

    def get_event_type(self) -> typing.Optional[type[hikari.MessageCreateEvent]]:
        """Get the base event type this mode listens to.

        Returns
        -------
        typing.Optional[type[hikari.message_events.MessageCreateEvent]]
            The type object of the MessageCreateEvent class this mode will
            register a listener for.

            This will be `None` if this mode disables listening to
            message create events/
        """
        return _ACCEPTS_EVENT_TYPE_MAPPING[self]


_ACCEPTS_EVENT_TYPE_MAPPING: dict[MessageAcceptsEnum, typing.Optional[type[hikari.MessageCreateEvent]]] = {
    MessageAcceptsEnum.ALL: hikari.MessageCreateEvent,
    MessageAcceptsEnum.DM_ONLY: hikari.DMMessageCreateEvent,
    MessageAcceptsEnum.GUILD_ONLY: hikari.GuildMessageCreateEvent,
    MessageAcceptsEnum.NONE: None,
}


def _check_human(ctx: tanjun_traits.Context, /) -> bool:
    return ctx.is_human


async def _wrap_client_callback(
    callback: tanjun_traits.MetaEventSig,
    args: tuple[str, ...],
    kwargs: dict[str, typing.Any],
    suppress_exceptions: bool,
) -> None:
    try:
        result = callback(*args, **kwargs)
        if isinstance(result, collections.Awaitable):
            await result

    except Exception as exc:
        if suppress_exceptions:
            _LOGGER.error("Client callback raised exception", exc_info=exc)

        else:
            raise


class _InjectablePrefixGetter(injecting.BaseInjectableValue[collections.Iterable[str]]):
    __slots__ = ()

    callback: PrefixGetterSig

    def __init__(
        self, callback: PrefixGetterSig, *, injector: typing.Optional[injecting.InjectorClient] = None
    ) -> None:
        super().__init__(callback, injector=injector)
        self.is_async = True

    async def __call__(self, ctx: tanjun_traits.Context, /) -> collections.Iterable[str]:
        return await self.call(ctx, ctx=ctx)


class Client(injecting.InjectorClient, tanjun_traits.Client):
    """Tanjun's standard `tanjun.traits.Client` implementation.

    This implementation supports dependency injection for checks, command
    callbacks, and prefix getters. For more information on how
    this works see `tanjun.injector`.

    !!! note
        For a quicker way to initiate this client around a standard bot aware
        client, see `Client.from_gateway_bot` and `Client.from_rest_bot`.

    Arguments
    ---------
    rest : hikari.api.rest.RestClient
        The Hikari REST client this will use.

    Other Parameters
    ----------------
    cache : hikari.api.cache.CacheClient
        The Hikari cache client this will use if applicable.
    event_manager : hikari.api.event_manager.EventManagerClient
        The Hikari event manager client this will use if applicable.

        !!! note
            This is necessary for message command dispatch and will also be
            necessary for interaction command dispatch if `server` isn't
            provided.
    server : hikari.api.interaction_server.InteractionServer
        The Hikari interaction server client this will use if applicable.

        !!! note
            This is used for interaction command dispatch if interaction
            events aren't being received from the event manager.
    shard : hikari.traits.ShardAware
        The Hikari shard aware client this will use if applicable.
    event_managed : bool
        Whether or not this client is managed by the event manager.

        An event managed client will be automatically started and closed based
        on Hikari's lifetime events.

        Defaults to `False` and can only be passed as `True` if `event_manager`
        is also provided.
    mention_prefix : bool
        Whether or not mention prefixes should be automatically set when this
        client is first started.

        Defaults to `False` and it should be noted that this only applies to
        message commands.
    set_global_commands : typing.Union[hikari.Snowflake, bool]
        Whether or not to automatically set global slash commands when this
        client is first started. Defaults to `False`.

        If a snowflake ID is passed here then the global commands will be
        set on this specific guild at startup rather than globally. This
        can be useful for testing/debug purposes as slash commands may take
        up to an hour to propogate globally but will immediately propogate
        when set on a specific guild.

    Raises
    ------
    ValueError
        If `event_managed` is `True` when `event_manager` is `None`.
    """

    __slots__ = (
        "_accepts",
        "_auto_defer_after",
        "_cache",
        "_cached_application_id",
        "_checks",
        "_client_callbacks",
        "_components",
        "_events",
        "_grab_mention_prefix",
        "_hooks",
        "_interaction_not_found",
        "_slash_hooks",
        "_is_alive",
        "_message_hooks",
        "_metadata",
        "_prefix_getter",
        "_prefixes",
        "_rest",
        "_server",
        "_shards",
    )

    def __init__(
        self,
        rest: hikari.api.RESTClient,
        cache: typing.Optional[hikari.api.Cache] = None,
        events: typing.Optional[hikari.api.EventManager] = None,
        server: typing.Optional[hikari.api.InteractionServer] = None,
        shard: typing.Optional[hikari_traits.ShardAware] = None,
        *,
        event_managed: bool = False,
        mention_prefix: bool = False,
        set_global_commands: typing.Union[hikari.Snowflake, bool] = False,
    ) -> None:
        # TODO: logging or something to indicate this is running statelessly rather than statefully.
        # TODO: warn if server and dispatch both None but don't error

        # TODO: separate slash and gateway checks?
        self._accepts = MessageAcceptsEnum.ALL if events else MessageAcceptsEnum.NONE
        self._auto_defer_after: typing.Optional[float] = 2.6
        self._cache = cache
        self._cached_application_id: typing.Optional[hikari.Snowflake] = None
        self._checks: set[injecting.InjectableCheck] = set()
        self._client_callbacks: dict[str, set[tanjun_traits.MetaEventSig]] = {}
        self._components: set[tanjun_traits.Component] = set()
        self._events = events
        self._grab_mention_prefix = mention_prefix
        self._hooks: typing.Optional[tanjun_traits.AnyHooks] = None
        self._interaction_not_found: typing.Optional[str] = "Command not found"
        self._slash_hooks: typing.Optional[tanjun_traits.SlashHooks] = None
        self._is_alive = False
        self._message_hooks: typing.Optional[tanjun_traits.MessageHooks] = None
        self._metadata: dict[typing.Any, typing.Any] = {}
        self._prefix_getter: typing.Optional[_InjectablePrefixGetter] = None
        self._prefixes: set[str] = set()
        self._rest = rest
        self._server = server
        self._shards = shard

        if event_managed:
            if not self._events:
                raise ValueError("Client cannot be event managed without an event manager")

            self._events.subscribe(hikari.StartingEvent, self._on_starting_event)
            self._events.subscribe(hikari.StoppingEvent, self._on_stopping_event)

        if set_global_commands:

            async def _set_global_commands_next_start() -> None:
                guild = (
                    hikari.UNDEFINED if isinstance(set_global_commands, bool) else hikari.Snowflake(set_global_commands)
                )
                await self.set_global_commands(guild=guild)
                self.remove_client_callback(tanjun_traits.ClientCallbackNames.STARTING, _set_global_commands_next_start)

            self.add_client_callback(
                tanjun_traits.ClientCallbackNames.STARTING,
                _set_global_commands_next_start,
            )

        # InjectorClient.__init__
        super().__init__(self)

    @classmethod
    def from_gateway_bot(
        cls,
        bot: hikari_traits.GatewayBotAware,
        /,
        *,
        event_managed: bool = True,
        mention_prefix: bool = False,
        set_global_commands: typing.Union[hikari.Snowflake, bool] = False,
    ) -> Client:
        """Build a `Client` from a `hikari.traits.GatewayBotAware` instance.

        !!! note
            This implicitly defaults the client to human only mode and also
            sets hikari trait injectors based on `bot`.

        Parameters
        ----------
        bot : hikari.traits.GatewayBotAware
            The bot client to build from.

            This will be used to infer the relevant Hikari clients to use.

        Other Parameters
        ----------------
        event_managed : bool
            Whether or not this client is managed by the event manager.

            An event managed client will be automatically started and closed
            based on Hikari's lifetime events.

            Defaults to `True`.
        mention_prefix : bool
            Whether or not mention prefixes should be automatically set when this
            client is first started.

            Defaults to `False` and it should be noted that this only applies to
            message commands.
        set_global_commands : typing.Union[hikari.Snowflake, bool] bool
            Whether or not to automatically set global slash commands when this
            client is first started. Defaults to `False`.

            If a snowflake ID is passed here then the global commands will be
            set on this specific guild at startup rather than globally. This
            can be useful for testing/debug purposes as slash commands may take
            up to an hour to propogate globally but will immediately propogate
            when set on a specific guild.
        """
        return (
            cls(
                rest=bot.rest,
                cache=bot.cache,
                events=bot.event_manager,
                shard=bot,
                event_managed=event_managed,
                mention_prefix=mention_prefix,
                set_global_commands=set_global_commands,
            )
            .set_human_only()
            .set_hikari_trait_injectors(bot)
        )

    @classmethod
    def from_rest_bot(
        cls,
        bot: hikari_traits.RESTBotAware,
        /,
        set_global_commands: typing.Union[hikari.Snowflake, bool] = False,
    ) -> Client:
        """Build a `Client` from a `hikari.traits.RESTBotAware` instance.

        !!! note
            This implicitly sets hikari trait injectors based on `bot`.

        Parameters
        ----------
        bot : hikari.traits.RESTBotAware
            The bot client to build from.

        Other Parameters
        ----------------
        set_global_commands : typing.Union[hikari.Snowflake, bool] bool
            Whether or not to automatically set global slash commands when this
            client is first started. Defaults to `False`.

            If a snowflake ID is passed here then the global commands will be
            set on this specific guild at startup rather than globally. This
            can be useful for testing/debug purposes as slash commands may take
            up to an hour to propogate globally but will immediately propogate
            when set on a specific guild.
        """
        return cls(
            rest=bot.rest, server=bot.interaction_server, set_global_commands=set_global_commands
        ).set_hikari_trait_injectors(bot)

    async def __aenter__(self) -> Client:
        await self.open()
        return self

    async def __aexit__(
        self,
        exception_type: typing.Optional[type[BaseException]],
        exception: typing.Optional[BaseException],
        exception_traceback: typing.Optional[types.TracebackType],
    ) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"CommandClient <{type(self).__name__!r}, {len(self._components)} components, {self._prefixes}>"

    @property
    def message_accepts(self) -> MessageAcceptsEnum:
        """The type of message create events this command client accepts for execution."""
        return self._accepts

    @property
    def is_human_only(self) -> bool:
        """Whether this client is only executing for non-bot/webhook users messages."""
        return _check_human in self._checks  # type: ignore[comparison-overlap]

    @property
    def cache(self) -> typing.Optional[hikari.api.Cache]:
        # <<inherited docstring from tanjun.traits.Client>>.
        return self._cache

    @property
    def checks(self) -> collections.Set[tanjun_traits.CheckSig]:
        """Set of the top level `tanjun.traits.Context` checks registered to this client.

        Returns
        -------
        collections.abc.Set[tanjun.traits.CheckSig]
            Set of the `tanjun.traits.Context` based checks registered for
            this client.

            These may be taking advantage of the standard dependency injection.
        """
        return {check.callback for check in self._checks}

    @property
    def components(self) -> collections.Set[tanjun_traits.Component]:
        # <<inherited docstring from tanjun.traits.Client>>.
        return self._components.copy()

    @property
    def events(self) -> typing.Optional[hikari.api.EventManager]:
        # <<inherited docstring from tanjun.traits.Client>>.
        return self._events

    @property
    def hooks(self) -> typing.Optional[tanjun_traits.AnyHooks]:
        """The top level `tanjun.traits.AnyHooks` set for this client.

        These are called during both message and interaction command execution.

        Returns
        -------
        typing.Optional[tanjun.traits.AnyHooks]
            The top level `tanjun.traits.Context` based hooks set for this
            client if applicable, else `None`.
        """
        return self._hooks

    @property
    def slash_hooks(self) -> typing.Optional[tanjun_traits.SlashHooks]:
        """The top level `tanjun.traits.SlashHooks` set for this client.

        These are only called during interaction command execution.

        Returns
        -------
        typing.Optional[tanjun.traits.SlashHooks]
            The top level `tanjun.traits.SlashContext` based hooks set
            for this client.
        """
        return self._slash_hooks

    @property
    def is_alive(self) -> bool:
        """Whether this client is alive."""
        return self._is_alive

    @property
    def message_hooks(self) -> typing.Optional[tanjun_traits.MessageHooks]:
        """The top level `tanjun.traits.MessageHooks` set for this client.

        These are only called during both message command execution.

        Returns
        -------
        typing.Optional[tanjun.traits.MessageHooks]
            The top level `tanjun.traits.MessageContext` based hooks set for
            this client.
        """
        return self._message_hooks

    @property
    def metadata(self) -> collections.MutableMapping[typing.Any, typing.Any]:
        # <<inherited docstring from tanjun.traits.Client>>.
        return self._metadata

    @property
    def prefix_getter(self) -> typing.Optional[PrefixGetterSig]:
        """Returns the prefix getter method set for this client.

        Returns
        -------
        typing.Optional[PrefixGetterSig]
            The prefix getter method set for this client if applicable,
            else `None`.

            For more information on this callback's signature see `PrefixGetter`.
        """
        return self._prefix_getter.callback if self._prefix_getter else None

    @property
    def prefixes(self) -> collections.Set[str]:
        """Set of the standard prefixes set for this client.

        Returns
        -------
        collections.abc.Set[str]
            The standard prefixes set for this client.
        """
        return self._prefixes.copy()

    @property
    def rest(self) -> hikari.api.RESTClient:
        # <<inherited docstring from tanjun.traits.Client>>.
        return self._rest

    @property
    def server(self) -> typing.Optional[hikari.api.InteractionServer]:
        # <<inherited docstring from tanjun.traits.Client>>.
        return self._server

    @property
    def shards(self) -> typing.Optional[hikari_traits.ShardAware]:
        # <<inherited docstring from tanjun.traits.Client>>.
        return self._shards

    async def _on_starting_event(self, _: hikari.StartingEvent, /) -> None:
        await self.open()

    async def _on_stopping_event(self, _: hikari.StoppingEvent, /) -> None:
        await self.close()

    def set_auto_defer_after(self: _ClientT, time: typing.Optional[float], /) -> _ClientT:
        """Set when this client should automatically defer execution of commands.

        Parameters
        ----------
        time : typing.Optional[float]
            The time in seconds to defer interaction command responses after.

            !!! note
                If this is set to ``None``, automatic deferals will be disabled.
                This may lead to unexpected behaviour.
        """
        self._auto_defer_after = float(time) if time is not None else None
        return self

    def set_hikari_trait_injectors(self: _ClientT, bot: hikari_traits.RESTAware, /) -> _ClientT:
        """Set type based dependency injection based on the hikari traits found in `bot`.

        This is a short hand for calling `Client.add_type_dependency` for all
        the hikari trait types `bot` is valid for with bot.

        Parameters
        ----------
        bot : hikari_traits.RESTAware
            The hikari client to set dependency injectors for.
        """
        for _, member in inspect.getmembers(hikari_traits):
            if inspect.isclass(member) and isinstance(bot, member):
                self.add_type_dependency(member, lambda: bot)

        return self

    def set_interaction_not_found(self: _ClientT, message: typing.Optional[str], /) -> _ClientT:
        """Set the response message for when an interaction command is not found.

        Parameters
        ----------
        message : typing.Optional[str]
            The message to respond with when an interaction command isn't found.

        !!! warning
            Setting this to `None` may lead to unexpected behaviour (especially
            when the client is still set to auto-defer interactions) and should
            only be done if you know what you're doing.
        """
        self._interaction_not_found = message
        return self

    def set_message_accepts(self: _ClientT, accepts: MessageAcceptsEnum, /) -> _ClientT:
        """Set the kind of messages commands should be executed based on.

        Parameters
        ----------
        accepts : MessageAcceptsEnum
            The type of messages commands should be executed based on.
        """
        if accepts.get_event_type() and not self._events:
            raise ValueError("Cannot set accepts level on a client with no event manager")

        self._accepts = accepts
        return self

    def set_human_only(self: _ClientT, value: bool = True) -> _ClientT:
        """Set whether or not message commands execution should be limited to "human" users.

        !!! note
            This doesn't apply to interaction commands as these can only be
            triggered by a "human" (normal user account).

        Parameters
        ----------
        value : bool
            Whether or not message commands execution should be limited to "human" users.

            Passing `True` here will prevent message commands from being executed
            based on webhook and bot messages.
        """
        if value:
            self.add_check(injecting.InjectableCheck(_check_human, injector=self))

        else:
            try:
                self.remove_check(_check_human)
            except ValueError:
                pass

        return self

    async def set_global_commands(
        self,
        application: typing.Optional[hikari.SnowflakeishOr[hikari.PartialApplication]] = None,
        /,
        *,
        guild: hikari.UndefinedOr[hikari.SnowflakeishOr[hikari.PartialGuild]] = hikari.UNDEFINED,
    ) -> collections.Sequence[hikari.Command]:
        """Set the global application commands for a bot based on the loaded components.

        !!! note
            This will overwrite any previously set application commands.

        Parameters
        ----------
        application : typing.Optional[hikari.SnowflakeishOr[hikari.PartialApplication]]
            Object or ID of the application to set the global commands for.

            If left as `None` then this will be inferred from the authorization
            being used by `Client.rest`.

        Other Parameters
        ----------------
        guild : hikari.UndefinedOr[hikari.SnowflakeishOr[hikari.PartialGuild]]
            Object or ID of the guild to set the global commands to.

            !!! note
                This can be useful for testing/debug purposes as slash commands
                may take up to an hour to propogate globally but will
                immediately propogate when set on a specific guild.

        Returns
        -------
        collections.abc.Sequence[hikari.interactions.command.Command]
            API representations of the set commands.
        """
        if not application:
            application = self._cached_application_id or await self.fetch_rest_application_id()

        found_top_names: set[str] = set()
        conflicts: set[str] = set()
        builders: list[hikari.api.CommandBuilder] = []

        for command in itertools.chain.from_iterable(component.slash_commands for component in self._components):
            if not command.is_global:
                continue

            if command.name in found_top_names:
                conflicts.add(command.name)

            found_top_names.add(command.name)
            builders.append(command.build())

        if conflicts:
            raise RuntimeError(
                "Couldn't set global commands due to conflicts. The following command names have more than one command "
                "registered for them " + ", ".join(conflicts)
            )

        commands = await self._rest.set_application_commands(application, builders, guild=guild)
        names_to_commands = {command.name: command for command in commands}
        for command in itertools.chain.from_iterable(component.slash_commands for component in self._components):
            if command.is_global:
                command.set_tracked_command(names_to_commands[command.name])

        return commands

    def add_check(self: _ClientT, check: tanjun_traits.CheckSig, /) -> _ClientT:
        self._checks.add(injecting.InjectableCheck(check, injector=self))
        return self

    def remove_check(self, check: tanjun_traits.CheckSig, /) -> None:
        self._checks.remove(check)  # type: ignore[arg-type]

    def with_check(self, check: tanjun_traits.CheckSigT, /) -> tanjun_traits.CheckSigT:
        self.add_check(check)
        return check

    async def check(self, ctx: tanjun_traits.Context, /) -> bool:
        return await utilities.gather_checks(ctx, self._checks)

    def add_component(self: _ClientT, component: tanjun_traits.Component, /, *, add_injector: bool = False) -> _ClientT:
        # <<inherited docstring from tanjun.traits.Client>>.
        if isinstance(component, injecting.Injectable):
            component.set_injector(self)

        component.bind_client(self)
        self._components.add(component)

        if add_injector:
            self.add_type_dependency(type(component), lambda: component)

        return self

    def remove_component(self, component: tanjun_traits.Component, /) -> None:
        # <<inherited docstring from tanjun.traits.Client>>.
        self._components.remove(component)
        component.unbind_client(self)

    def add_client_callback(self: _ClientT, event_name: str, callback: tanjun_traits.MetaEventSig, /) -> _ClientT:
        # <<inherited docstring from tanjun.traits.Client>>.
        event_name = event_name.lower()
        try:
            self._client_callbacks[event_name].add(callback)
        except KeyError:
            self._client_callbacks[event_name] = {callback}

        return self

    async def dispatch_client_callback(
        self, event_name: str, /, *args: typing.Any, suppress_exceptions: bool = True, **kwargs: typing.Any
    ) -> None:
        event_name = event_name.lower()
        if callbacks := self._client_callbacks.get(event_name):
            await asyncio.gather(
                *(_wrap_client_callback(callback, args, kwargs, suppress_exceptions) for callback in callbacks)
            )

    def get_client_callbacks(self, event_name: str, /) -> collections.Collection[tanjun_traits.MetaEventSig]:
        # <<inherited docstring from tanjun.traits.Client>>.
        event_name = event_name.lower()
        return self._client_callbacks.get(event_name) or ()

    def remove_client_callback(self, event_name: str, callback: tanjun_traits.MetaEventSig, /) -> None:
        # <<inherited docstring from tanjun.traits.Client>>.
        event_name = event_name.lower()
        self._client_callbacks[event_name].remove(callback)
        if not self._client_callbacks[event_name]:
            del self._client_callbacks[event_name]

    def with_client_callback(
        self, event_name: str, /
    ) -> collections.Callable[[tanjun_traits.MetaEventSigT], tanjun_traits.MetaEventSigT]:
        # <<inherited docstring from tanjun.traits.Client>>.
        def decorator(callback: tanjun_traits.MetaEventSigT, /) -> tanjun_traits.MetaEventSigT:
            self.add_client_callback(event_name, callback)
            return callback

        return decorator

    def add_prefix(self: _ClientT, prefixes: typing.Union[collections.Iterable[str], str], /) -> _ClientT:
        if isinstance(prefixes, str):
            self._prefixes.add(prefixes)

        else:
            self._prefixes.update(prefixes)

        return self

    def remove_prefix(self, prefix: str, /) -> None:
        self._prefixes.remove(prefix)

    def set_prefix_getter(self: _ClientT, getter: typing.Optional[PrefixGetterSig], /) -> _ClientT:
        self._prefix_getter = _InjectablePrefixGetter(getter, injector=self) if getter else None
        return self

    def with_prefix_getter(self, getter: PrefixGetterSigT, /) -> PrefixGetterSigT:
        self.set_prefix_getter(getter)
        return getter

    def check_message_context(
        self, ctx: tanjun_traits.MessageContext, /
    ) -> collections.AsyncIterator[tuple[str, tanjun_traits.MessageCommand]]:
        # <<inherited docstring from tanjun.traits.Client>>.
        return utilities.async_chain(component.check_message_context(ctx) for component in self._components)

    def check_message_name(self, name: str, /) -> collections.Iterator[tuple[str, tanjun_traits.MessageCommand]]:
        # <<inherited docstring from tanjun.traits.Client>>.
        return itertools.chain.from_iterable(component.check_message_name(name) for component in self._components)

    async def _check_prefix(self, ctx: tanjun_traits.MessageContext, /) -> typing.Optional[str]:
        if self._prefix_getter:
            for prefix in await self._prefix_getter(ctx):
                if ctx.content.startswith(prefix):
                    return prefix

        for prefix in self._prefixes:
            if ctx.content.startswith(prefix):
                return prefix

        return None

    def _try_unsubscribe(
        self,
        event_manager: hikari.api.EventManager,
        event_type: type[event_manager_api.EventT_inv],
        callback: event_manager_api.CallbackT[event_manager_api.EventT_inv],
    ) -> None:
        try:
            event_manager.unsubscribe(event_type, callback)
        except (ValueError, LookupError):
            # TODO: add logging here
            pass

    async def close(self, *, deregister_listener: bool = True) -> None:
        if not self._is_alive:
            raise RuntimeError("Client isn't active")

        await self.dispatch_client_callback(tanjun_traits.ClientCallbackNames.CLOSING)
        if deregister_listener and self._events:
            if event_type := self._accepts.get_event_type():
                self._try_unsubscribe(self._events, event_type, self.on_message_create_event)

            self._try_unsubscribe(self._events, hikari.InteractionCreateEvent, self.on_interaction_create_event)

            if self._server:
                self._server.set_listener(hikari.CommandInteraction, None)
        await self.dispatch_client_callback(tanjun_traits.ClientCallbackNames.CLOSED)

    async def open(self, *, register_listener: bool = True) -> None:
        if self._is_alive:
            raise RuntimeError("Client is already alive")

        await self.dispatch_client_callback(tanjun_traits.ClientCallbackNames.STARTING, suppress_exceptions=False)
        self._is_alive = True
        if self._grab_mention_prefix:
            user: typing.Optional[hikari.User] = None
            if self._cache:
                user = self._cache.get_me()

            if not user:
                retry = backoff.Backoff(max_retries=4, maximum=30)

                async for _ in retry:
                    try:
                        user = await self._rest.fetch_my_user()
                        break

                    except (hikari.RateLimitedError, hikari.RateLimitTooLongError) as exc:
                        if exc.retry_after > 30:
                            raise

                        retry.set_next_backoff(exc.retry_after)

                    except hikari.InternalServerError:
                        continue

                else:
                    user = await self._rest.fetch_my_user()

            self._prefixes.add(f"<@{user.id}>")
            self._prefixes.add(f"<@!{user.id}>")
            self._grab_mention_prefix = False

        if register_listener and self._events:
            if event_type := self._accepts.get_event_type():
                self._events.subscribe(event_type, self.on_message_create_event)

            self._events.subscribe(hikari.InteractionCreateEvent, self.on_interaction_create_event)

        if self._server:
            self._server.set_listener(hikari.CommandInteraction, self.on_interaction_create_request)

        asyncio.create_task(
            self.dispatch_client_callback(tanjun_traits.ClientCallbackNames.STARTED, suppress_exceptions=False)
        )

    async def fetch_rest_application_id(self) -> hikari.Snowflake:
        if self._cached_application_id:
            return self._cached_application_id

        if self._rest.token_type == hikari.TokenType.BOT:
            application = await self._rest.fetch_application()

        else:
            application = (await self._rest.fetch_authorization()).application

        self._cached_application_id = hikari.Snowflake(application)
        return self._cached_application_id

    def set_hooks(self: _ClientT, hooks: typing.Optional[tanjun_traits.AnyHooks], /) -> _ClientT:
        self._hooks = hooks
        return self

    def set_slash_hooks(self: _ClientT, hooks: typing.Optional[tanjun_traits.SlashHooks], /) -> _ClientT:
        self._slash_hooks = hooks
        return self

    def set_message_hooks(self: _ClientT, hooks: typing.Optional[tanjun_traits.MessageHooks], /) -> _ClientT:
        self._message_hooks = hooks
        return self

    def load_modules(self: _ClientT, *modules: typing.Union[str, pathlib.Path]) -> _ClientT:
        for module_path in modules:
            if isinstance(module_path, str):
                module = importlib.import_module(module_path)

            else:
                spec = importlib_util.spec_from_file_location(
                    module_path.name.rsplit(".", 1)[0], str(module_path.absolute())
                )

                # https://github.com/python/typeshed/issues/2793
                if spec and isinstance(spec.loader, importlib_abc.Loader):
                    module = importlib_util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                raise RuntimeError(f"Unknown or invalid module provided {module_path}")

            for _, member in inspect.getmembers(module):
                if isinstance(member, _LoadableDescriptor):
                    member(self)

        return self

    async def on_message_create_event(self, event: hikari.MessageCreateEvent, /) -> None:
        if event.message.content is None:
            return

        ctx = context.MessageContext(self, content=event.message.content, message=event.message)
        if (prefix := await self._check_prefix(ctx)) is None:
            return

        ctx.set_content(ctx.content.lstrip()[len(prefix) :].lstrip()).set_triggering_prefix(prefix)
        if not await self.check(ctx):
            return

        hooks: typing.Optional[set[tanjun_traits.MessageHooks]] = None
        if self._hooks and self._message_hooks:
            hooks = {self._hooks, self._message_hooks}

        elif self._hooks:
            hooks = {self._hooks}

        elif self._message_hooks:
            hooks = {self._message_hooks}

        try:
            for component in self._components:
                if await component.execute_message(ctx, hooks=hooks):
                    break

        except errors.HaltExecution:
            pass

        except errors.CommandError as exc:
            await ctx.respond(exc.message)
            return

        await self.dispatch_client_callback(tanjun_traits.ClientCallbackNames.MESSAGE_COMMAND_NOT_FOUND, ctx)

    def _get_slash_hooks(self) -> typing.Optional[set[tanjun_traits.SlashHooks]]:
        hooks: typing.Optional[set[tanjun_traits.SlashHooks]] = None
        if self._hooks and self._slash_hooks:
            hooks = {self._hooks, self._slash_hooks}

        elif self._hooks:
            hooks = {self._hooks}

        elif self._slash_hooks:
            hooks = {self._slash_hooks}

        return hooks

    async def on_interaction_create_event(self, event: hikari.InteractionCreateEvent, /) -> None:
        """Listener function for executing slash commands based on Gateway events.

        Parameters
        ----------
        event : hikari.events.interaction_events.InteractionCreateEvent
            The event to execute commands based on.

            !!! note
                Any event where `event.interaction` is not
                `hikari.interactions.commands.CommandInteraction` will be ignored.
        """
        if not isinstance(event.interaction, hikari.CommandInteraction):
            return

        ctx = context.SlashContext(self, event.interaction, not_found_message=self._interaction_not_found)
        hooks = self._get_slash_hooks()

        if self._auto_defer_after is not None:
            ctx.start_defer_timer(self._auto_defer_after)

        try:
            for component in self._components:
                if future := await component.execute_interaction(ctx, hooks=hooks):
                    await future
                    return

        except errors.HaltExecution:
            pass

        except errors.CommandError as exc:
            await ctx.respond(exc.message)
            return

        await self.dispatch_client_callback(tanjun_traits.ClientCallbackNames.SLASH_COMMAND_NOT_FOUND, ctx)
        await ctx.mark_not_found()
        ctx.cancel_defer()

    async def on_interaction_create_request(self, interaction: hikari.CommandInteraction, /) -> context.ResponseTypeT:
        """Listener function for executing slash commands based on received REST requests.

        Parameters
        ----------
        interaction : hikari.interactions.commands.CommandInteraction
            The interaction to execute a command based on.

        Returns
        -------
        tanjun.context.ResponseType
            The initial response to send back to Discord.
        """
        ctx = context.SlashContext(self, interaction, not_found_message=self._interaction_not_found)
        if self._auto_defer_after is not None:
            ctx.start_defer_timer(self._auto_defer_after)

        hooks = self._get_slash_hooks()
        future = ctx.get_response_future()
        try:
            for component in self._components:
                if await component.execute_interaction(ctx, hooks=hooks):
                    return await future

        except errors.HaltExecution:
            pass

        except errors.CommandError as exc:
            # Under very specific timing there may be another future which could set a result while we await
            # ctx.respond therefore we create a task to avoid any erronous behaviour from this trying to create
            # another response before it's returned the initial response.
            asyncio.create_task(ctx.respond(exc.message))
            return await future

        async def callback(_: asyncio.Future[None]) -> None:
            await ctx.mark_not_found()
            ctx.cancel_defer()

        asyncio.create_task(
            self.dispatch_client_callback(tanjun_traits.ClientCallbackNames.SLASH_COMMAND_NOT_FOUND, ctx)
        ).add_done_callback(callback)
        return await future
