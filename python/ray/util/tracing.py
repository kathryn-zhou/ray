from contextlib import contextmanager
from functools import wraps
import inspect
import logging
import os
from typing import (
    Any,
    cast,
    Callable,
    Dict,
    Generator,
    MutableMapping,
    Optional,
    Sequence,
    Union,
)

from ray.runtime_context import get_runtime_context
from ray.util.inspect import is_function_or_method
import ray.worker

logger = logging.getLogger(__name__)

try:
    from opentelemetry import context, trace
    from opentelemetry.context.context import Context
    from opentelemetry.util.types import Attributes
    from opentelemetry import propagators
    from opentelemetry.trace.propagation.textmap import DictGetter
except ImportError:
    if os.getenv("RAY_TRACING_ENABLED", "False").lower() in ["true", "1"]:
        logger.warning(
            "Install opentelemetry with 'pip install opentelemetry-api' and"
            "'pip install opentelemetry-sdk' to enable tracing.")

_nameable = Union[str, Callable[..., Any]]
_global_is_tracing_enabled = None


def is_tracing_enabled() -> bool:
    """Checks environment variable feature flag to see if tracing is turned on.
    Tracing is off by default."""
    global _global_is_tracing_enabled
    if _global_is_tracing_enabled is None:
        _global_is_tracing_enabled = os.getenv(
            "RAY_TRACING_ENABLED", "False").lower() in ["true", "1"]
    return _global_is_tracing_enabled


class DictPropagator:
    def inject_current_context() -> Dict[Any, Any]:
        """Inject trace context into otel propagator."""
        context_dict: Dict[Any, Any] = {}
        propagators.inject(dict.__setitem__, context_dict)
        return context_dict

    def extract(context_dict: Dict[Any, Any]) -> Context:
        """Given a trace context, extract as a Context."""
        return cast(Context, propagators.extract(DictGetter(), context_dict))


@contextmanager
def use_context(parent_context: Context) -> Generator[None, None, None]:
    """Uses the Ray trace context for the span."""
    new_context = parent_context if parent_context is not None else Context()
    token = context.attach(new_context)
    try:
        yield
    finally:
        context.detach(token)


def _function_hydrate_span_args(func: Callable[..., Any]) -> Attributes:
    """Get the attributes of the function that will be reported as attributes
    in the trace."""
    runtime_context = get_runtime_context().get()

    span_args = {
        "ray.remote": "function",
        "ray.function": func,
        "ray.pid": str(os.getpid()),
        "ray.job_id": runtime_context["job_id"].hex(),
        "ray.node_id": runtime_context["node_id"].hex(),
    }

    # We only get task ID for workers
    if ray.worker.global_worker.mode == ray.worker.WORKER_MODE:
        task_id = (runtime_context["task_id"].hex()
                   if runtime_context["task_id"] else None)
        if task_id:
            span_args["ray.task_id"] = task_id

    worker_id = getattr(ray.worker.global_worker, "worker_id", None)
    if worker_id:
        span_args["ray.worker_id"] = worker_id.hex()

    return span_args


def _function_span_producer_name(func: Callable[..., Any]) -> str:
    """Returns the function span name that has span kind of producer."""
    args = _function_hydrate_span_args(func)
    assert args is not None
    name = args["ray.function"]

    return f"{name} ray.remote"


def _function_span_consumer_name(func: Callable[..., Any]) -> str:
    """Returns the function span name that has span kind of consumer."""
    args = _function_hydrate_span_args(func)
    assert args is not None
    name = args["ray.function"]

    return f"{name} ray.remote_worker"


def _actor_hydrate_span_args(class_: _nameable,
                             method: _nameable) -> Attributes:
    """Get the attributes of the actor that will be reported as attributes
    in the trace."""
    if callable(class_):
        class_ = class_.__name__
    if callable(method):
        method = method.__name__

    runtime_context = get_runtime_context().get()

    span_args = {
        "ray.remote": "actor",
        "ray.actor_class": class_,
        "ray.actor_method": method,
        "ray.function": f"{class_}.{method}",
        "ray.pid": str(os.getpid()),
        "ray.job_id": runtime_context["job_id"].hex(),
        "ray.node_id": runtime_context["node_id"].hex(),
    }

    # We only get actor ID for workers
    if ray.worker.global_worker.mode == ray.worker.WORKER_MODE:
        actor_id = (runtime_context["actor_id"].hex()
                    if runtime_context["actor_id"] else None)

        if actor_id:
            span_args["ray.actor_id"] = actor_id

    worker_id = getattr(ray.worker.global_worker, "worker_id", None)
    if worker_id:
        span_args["ray.worker_id"] = worker_id.hex()

    return span_args


def _actor_span_producer_name(class_: _nameable, method: _nameable) -> str:
    """Returns the actor span name that has span kind of producer."""
    args = _actor_hydrate_span_args(class_, method)
    assert args is not None
    name = args["ray.function"]

    return f"{name} ray.remote"


def _actor_span_consumer_name(class_: _nameable, method: _nameable) -> str:
    """Returns the actor span name that has span kind of consumer."""
    args = _actor_hydrate_span_args(class_, method)
    assert args is not None
    name = args["ray.function"]

    return f"{name} ray.remote_worker"


def _tracing_task_invocation(method):
    """Trace the execution of a remote task. Inject
    the current span context into kwargs for propagation."""

    @wraps(method)
    def _invocation_remote_span(
            self,
            args: Any,  # from tracing
            kwargs: MutableMapping[Any, Any],  # from tracing
            *_args: Any,  # from Ray
            **_kwargs: Any,  # from Ray
    ) -> Any:
        # If tracing feature flag is not on, perform a no-op
        if not is_tracing_enabled():
            return method(self, args, kwargs, *_args, **_kwargs)
        assert "_ray_trace_ctx" not in kwargs
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(
                _function_span_producer_name(self._function_name),
                kind=trace.SpanKind.PRODUCER,
                attributes=_function_hydrate_span_args(self._function_name),
        ):
            # Inject a _ray_trace_ctx as a dictionary
            kwargs["_ray_trace_ctx"] = DictPropagator.inject_current_context()
            return method(self, args, kwargs, *_args, **_kwargs)

    return _invocation_remote_span


def _inject_tracing_into_function(function):
    """Wrap the function argument passed to RemoteFunction's __init__ so that
    future execution of that function will include tracing.
    Use the provided trace context from kwargs.
    """

    def _function_with_tracing(
            *args: Any,
            _ray_trace_ctx: Optional[Dict[str, Any]] = None,
            **kwargs: Any,
    ) -> Any:
        # If tracing feature flag is not on, perform a no-op
        if not is_tracing_enabled():
            return function(*args, **kwargs)

        tracer = trace.get_tracer(__name__)

        assert (
            _ray_trace_ctx is not None,  # noqa: F631
            f"Missing ray_trace_ctx!: {args}, {kwargs}")

        function_name = function.__module__ + "." + function.__name__

        # Retrieves the context from the _ray_trace_ctx dictionary we injected
        with use_context(DictPropagator.extract(
                _ray_trace_ctx)), tracer.start_as_current_span(
                    _function_span_consumer_name(function_name),
                    kind=trace.SpanKind.CONSUMER,
                    attributes=_function_hydrate_span_args(function_name),
                ):
            return function(*args, **kwargs)

    return _function_with_tracing


def _tracing_actor_creation(method):
    """Trace the creation of an actor. Inject
    the current span context into kwargs for propagation."""

    @wraps(method)
    def _invocation_actor_class_remote_span(
            self,
            args: Any,  # from tracing
            kwargs: MutableMapping[Any, Any],  # from tracing
            *_args: Any,  # from Ray
            **_kwargs: Any,  # from Ray
    ):
        # If tracing feature flag is not on, perform a no-op
        if not is_tracing_enabled():
            return method(self, args, kwargs, *_args, **_kwargs)

        class_name = self.__ray_metadata__.class_name
        method_name = "__init__"
        assert "_ray_trace_ctx" not in _kwargs
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(
                name=_actor_span_producer_name(class_name, method_name),
                kind=trace.SpanKind.PRODUCER,
                attributes=_actor_hydrate_span_args(class_name, method_name),
        ) as span:
            # Inject a _ray_trace_ctx as a dictionary
            kwargs["_ray_trace_ctx"] = DictPropagator.inject_current_context()

            result = method(self, args, kwargs, *_args, **_kwargs)

            span.set_attribute("ray.actor_id", result._ray_actor_id.hex())

            return result

    return _invocation_actor_class_remote_span


def _tracing_actor_method_invocation(method):
    """Trace the invocation of an actor method."""

    @wraps(method)
    def _start_span(
            self,
            args: Sequence[Any],
            kwargs: MutableMapping[Any, Any],
            *_args: Any,
            **_kwargs: Any,
    ) -> Any:
        # If tracing feature flag is not on, perform a no-op
        if not is_tracing_enabled():
            return method(self, args, kwargs, *_args, **_kwargs)

        class_name = (self._actor_ref()
                      ._ray_actor_creation_function_descriptor.class_name)
        method_name = self._method_name
        assert "_ray_trace_ctx" not in _kwargs

        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(
                name=_actor_span_producer_name(class_name, method_name),
                kind=trace.SpanKind.PRODUCER,
                attributes=_actor_hydrate_span_args(class_name, method_name),
        ) as span:
            # Inject a _ray_trace_ctx as a dictionary
            kwargs["_ray_trace_ctx"] = DictPropagator.inject_current_context()

            span.set_attribute("ray.actor_id",
                               self._actor_ref()._ray_actor_id.hex())

            return method(self, args, kwargs, *_args, **_kwargs)

    return _start_span


def _inject_tracing_into_class(_cls):
    """Given a class that will be made into an actor,
    inject tracing into all of the methods."""

    def span_wrapper(method: Callable[..., Any]) -> Any:
        def _resume_span(
                self: Any,
                *_args: Any,
                _ray_trace_ctx: Optional[Dict[str, Any]] = None,
                **_kwargs: Any,
        ) -> Any:
            """
            Wrap the user's function with a function that
            will extract the trace context
            """
            tracer: trace.Tracer = trace.get_tracer(__name__)

            # Retrieves the context from the _ray_trace_ctx dictionary we
            # injected, or starts a new context
            if _ray_trace_ctx:
                with use_context(DictPropagator.extract(
                        _ray_trace_ctx)), tracer.start_as_current_span(
                            _actor_span_consumer_name(self.__class__.__name__,
                                                      method),
                            kind=trace.SpanKind.CONSUMER,
                            attributes=_actor_hydrate_span_args(
                                self.__class__.__name__, method),
                        ):
                    return method(self, *_args, **_kwargs)
            else:
                with tracer.start_as_current_span(
                        _actor_span_consumer_name(self.__class__.__name__,
                                                  method),
                        kind=trace.SpanKind.CONSUMER,
                        attributes=_actor_hydrate_span_args(
                            self.__class__.__name__, method),
                ):
                    return method(self, *_args, **_kwargs)

        return _resume_span

    def async_span_wrapper(method: Callable[..., Any]) -> Any:
        async def _resume_span(
                self: Any,
                *_args: Any,
                _ray_trace_ctx: Optional[Dict[str, Any]] = None,
                **_kwargs: Any,
        ) -> Any:
            """
            Wrap the user's function with a function that
            will extract the trace context
            """
            tracer = trace.get_tracer(__name__)

            # Retrieves the context from the _ray_trace_ctx dictionary we
            # injected, or starts a new context
            if _ray_trace_ctx:
                with use_context(DictPropagator.extract(
                        _ray_trace_ctx)), tracer.start_as_current_span(
                            _actor_span_consumer_name(self.__class__.__name__,
                                                      method.__name__),
                            kind=trace.SpanKind.CONSUMER,
                            attributes=_actor_hydrate_span_args(
                                self.__class__.__name__, method.__name__),
                        ):
                    return await method(self, *_args, **_kwargs)
            else:
                with tracer.start_as_current_span(
                        _actor_span_consumer_name(self._wrapped.__name__,
                                                  method.__name__),
                        kind=trace.SpanKind.CONSUMER,
                        attributes=_actor_hydrate_span_args(
                            self._wrapped.__name__, method.__name__),
                ):
                    return await method(self, *_args, **_kwargs)

        return _resume_span

    # If tracing feature flag is not on, perform a no-op
    if not is_tracing_enabled():
        return _cls

    methods = inspect.getmembers(_cls, is_function_or_method)
    for name, method in methods:
        if inspect.iscoroutinefunction(method):
            # If the method was async, swap out sync wrapper into async
            wrapped_method = async_span_wrapper(method)
        else:
            wrapped_method = span_wrapper(method)

        setattr(_cls, name, wrapped_method)

    return _cls
