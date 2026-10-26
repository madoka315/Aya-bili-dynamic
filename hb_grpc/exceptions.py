class GrpcError(Exception):
    """B站 gRPC 返回的业务错误（如 -352 风控）"""

    def __init__(self, code, msg):
        self.code = code
        self.msg = msg

    def __repr__(self):
        return f"错误码: {self.code}, 信息: {self.msg}"

    __str__ = __repr__
