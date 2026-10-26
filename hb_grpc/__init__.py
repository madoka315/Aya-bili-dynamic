"""B站 App gRPC 最小客户端（动态列表），裁剪自 bilireq 0.2.6（GPL-3.0）。"""
from functools import wraps
from typing import List, Optional, Tuple
from typing import Any

import grpc
from google.protobuf.json_format import MessageToDict

from .exceptions import GrpcError
from .metadata import make_metadata
from .protos.bilibili.app.dynamic.v2.dynamic_pb2 import (
    DynSpaceReq,
    DynSpaceRsp,
    DynamicType,
)
from .protos.bilibili.app.dynamic.v2.dynamic_pb2_grpc import DynamicStub
from .protos.bilibili.rpc.status_pb2 import Status

GRPC_HOST = "grpc.biliapi.net"

__all__ = ["get_user_dynamics", "make_metadata", "GrpcError", "DynamicType", "GRPC_HOST"]


async def get_user_dynamics(
    uid, *, access_key: str = "", proxy: str = "", timeout: int = 10
) -> DynSpaceRsp:
    """拉取 UP 主空间动态列表。

    游客可用；配置 access_key 后请求携带 App 登录态（风控收紧时的降级手段）。
    被风控软门控时返回空 Rsp（list 为空），不抛异常；硬错误抛 GrpcError。
    """
    options: List[Tuple[str, Any]] = [("grpc.http_proxy", proxy or "")]
    async with grpc.aio.secure_channel(
        GRPC_HOST, grpc.ssl_channel_credentials(), options=options
    ) as channel:
        try:
            stub = DynamicStub(channel)
            req = DynSpaceReq(host_uid=int(uid), history_offset="", page=1)
            return await stub.DynSpace(
                req, metadata=make_metadata(access_key or None), timeout=timeout
            )
        except grpc.RpcError as e:
            status = None
            try:
                tm = e.trailing_metadata()
                if tm is not None:
                    status = tm.get("grpc-status-details-bin")
            except Exception:
                status = None
            if not status:
                raise
            status_dict = MessageToDict(Status.FromString(status))
            code = status_dict.get("code")
            if code == 2:
                details = status_dict.get("details") or [{}]
                raise GrpcError(details[0].get("code"), details[0].get("message"))
            raise GrpcError(code, status_dict.get("message"))
