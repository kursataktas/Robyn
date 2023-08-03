import inspect
from abc import ABC, abstractmethod
from asyncio import iscoroutinefunction
from functools import wraps
from inspect import signature
from types import CoroutineType
from typing import Callable, Dict, List, NamedTuple, Union, Optional
from robyn.authentication import AuthenticationHandler, AuthenticationNotConfiguredError
import inspect
from robyn.robyn import FunctionInfo, HttpMethod, MiddlewareType, Request, Response
from robyn import status_codes
from robyn.ws import WS


class Route(NamedTuple):
    route_type: HttpMethod
    route: str
    function: FunctionInfo
    is_const: bool


class RouteMiddleware(NamedTuple):
    middleware_type: MiddlewareType
    route: str
    function: FunctionInfo


class GlobalMiddleware(NamedTuple):
    middleware_type: MiddlewareType
    function: FunctionInfo


class BaseRouter(ABC):
    @abstractmethod
    def add_route(*args) -> Union[Callable, CoroutineType, WS]:
        ...


class Router(BaseRouter):
    def __init__(self) -> None:
        super().__init__()
        self.routes: List[Route] = []

    def _format_response(self, res):
        response = {}
        if isinstance(res, dict):
            status_code = res.get("status_code", status_codes.HTTP_200_OK)
            headers = res.get("headers", {"Content-Type": "text/plain"})
            description = res.get("description", "")

            if type(status_code) != int:
                status_code = int(status_code)  # status_code can potentially be string

            response = Response(status_code=status_code, headers=headers, body=body)
            file_path = res.get("file_path")
            if file_path is not None:
                response.file_path = file_path
        elif isinstance(res, Response):
            response = res
        elif isinstance(res, bytes):
            response = Response(
                status_code=status_codes.HTTP_200_OK,
                headers={"Content-Type": "application/octet-stream"},
                body=res,
            )
        else:
            response = Response(
                status_code=status_codes.HTTP_200_OK,
                headers={"Content-Type": "text/plain"},
                body=str(res).encode("utf-8"),
            )
        return response

    def validate_handler_args(self, handler, endpoint, dependencies):
        #Ensure handler function arguments match provided dependencies for an endpoint.
        handler_args = (inspect.signature(handler)).parameters.values()
        param_list = [a.name for a in handler_args]
        dependency_dict = {**dependencies.get(endpoint, {}), **dependencies["ALL_ROUTES"]}
        for param in param_list:
            if param not in dependency_dict:
                raise ValueError(
                    f"Arguments of the {handler.__name__} function do not match the dependencies provided for the {endpoint} endpoint. Please check the dependencies provided for the {endpoint} endpoint and try again. {param_list} {dependency_dict}"
                )
        return param_list, dependency_dict
    
    def add_route(
        self,
        route_type: HttpMethod,
        endpoint: str,
        handler: Callable,
        is_const: bool,
        dependencies: Dict[str, any],
        exception_handler: Optional[Callable],
    ) -> Union[Callable, CoroutineType]:
        @wraps(handler)
        async def async_inner_handler(*args):
            param_list, dependency_dict = self.validate_handler_args(handler, endpoint, dependencies)
            # dependencies_to_pass construction considers each parameter specified in the handler function
            #'request' specified in init's dep mapping lets this construction account for a request parameter in the handler function
            dependencies_to_pass = [
                dependency_dict[key] for key in param_list if key in dependency_dict
            ]
            try:
                response = self._format_response(await handler(*dependencies_to_pass))
            except Exception as err:
                if exception_handler is None:
                    raise
                response = self._format_response(exception_handler(err))
            return response

        @wraps(handler)
        def inner_handler(*args):
            param_list, dependency_dict = self.validate_handler_args(handler, endpoint, dependencies)

            for param in param_list:
                if param not in dependency_dict:
                    raise ValueError(
                        f"Arguments of the {handler.__name__} function do not match the dependencies provided for the {endpoint} endpoint. Please check the dependencies provided for the {endpoint} endpoint and try again. {param_list} {dependency_dict}"
                    )

            # dependencies_to_pass construction considers each parameter specified in the handler function
            #'request' specified in init's dep mapping lets this construction account for a request parameter in the handler function
            dependencies_to_pass = [
                dependency_dict[key] for key in param_list if key in dependency_dict
            ]

            try:
                response = self._format_response(handler(*dependencies_to_pass))
            except Exception as err:
                if exception_handler is None:
                    raise
                response = self._format_response(exception_handler(err))
            return response

        number_of_params = len(signature(handler).parameters)
        if iscoroutinefunction(handler):
            function = FunctionInfo(async_inner_handler, True, number_of_params)
            self.routes.append(Route(route_type, endpoint, function, is_const))
            return async_inner_handler
        else:
            function = FunctionInfo(inner_handler, False, number_of_params)
            self.routes.append(Route(route_type, endpoint, function, is_const))
            return inner_handler

    def get_routes(self) -> List[Route]:
        return self.routes


class MiddlewareRouter(BaseRouter):
    def __init__(self) -> None:
        super().__init__()
        self.global_middlewares: List[GlobalMiddleware] = []
        self.route_middlewares: List[RouteMiddleware] = []
        self.authentication_handler: Optional[AuthenticationHandler] = None

    def set_authentication_handler(self, authentication_handler: AuthenticationHandler):
        self.authentication_handler = authentication_handler

    def add_route(
        self, middleware_type: MiddlewareType, endpoint: str, handler: Callable
    ) -> Callable:
        number_of_params = len(signature(handler).parameters)
        function = FunctionInfo(handler, iscoroutinefunction(handler), number_of_params)
        self.route_middlewares.append(
            RouteMiddleware(middleware_type, endpoint, function)
        )
        return handler

    def add_auth_middleware(self, endpoint: str):
        """
        This method adds an authentication middleware to the specified endpoint.
        """

        def inner(handler):
            def inner_handler(request: Request, *args):
                if not self.authentication_handler:
                    raise AuthenticationNotConfiguredError()
                identity = self.authentication_handler.authenticate(request)
                if identity is None:
                    return self.authentication_handler.unauthorized_response
                request.identity = identity
                return request

            self.add_route(MiddlewareType.BEFORE_REQUEST, endpoint, inner_handler)
            return inner_handler

        return inner

    # These inner functions are basically a wrapper around the closure(decorator) being returned.
    # They take a handler, convert it into a closure and return the arguments.
    # Arguments are returned as they could be modified by the middlewares.
    def add_middleware(
        self, middleware_type: MiddlewareType, endpoint: Optional[str]
    ) -> Callable[..., None]:
        def inner(handler):
            @wraps(handler)
            async def async_inner_handler(*args):
                return await handler(*args)

            @wraps(handler)
            def inner_handler(*args):
                return handler(*args)

            if endpoint is not None:
                if iscoroutinefunction(handler):
                    self.add_route(middleware_type, endpoint, async_inner_handler)
                else:
                    self.add_route(middleware_type, endpoint, inner_handler)
            else:
                if iscoroutinefunction(handler):
                    self.global_middlewares.append(
                        GlobalMiddleware(
                            middleware_type,
                            FunctionInfo(
                                async_inner_handler,
                                True,
                                len(signature(async_inner_handler).parameters),
                            ),
                        )
                    )
                else:
                    self.global_middlewares.append(
                        GlobalMiddleware(
                            middleware_type,
                            FunctionInfo(
                                inner_handler,
                                False,
                                len(signature(inner_handler).parameters),
                            ),
                        )
                    )

        return inner

    def get_route_middlewares(self) -> List[RouteMiddleware]:
        return self.route_middlewares

    def get_global_middlewares(self) -> List[GlobalMiddleware]:
        return self.global_middlewares


class WebSocketRouter(BaseRouter):
    def __init__(self) -> None:
        super().__init__()
        self.routes = {}

    def add_route(self, endpoint: str, web_socket: WS) -> None:
        self.routes[endpoint] = web_socket

    def get_routes(self) -> Dict[str, WS]:
        return self.routes
