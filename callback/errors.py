"""业务错误类型。"""


class BusinessError(Exception):
    """规则校验失败（如承诺时间落入禁扰时段）。"""


class DuplicatePromiseError(BusinessError):
    def __init__(self, existing_id: int):
        super().__init__(f"该客户已有未关闭承诺 #{existing_id}")
        self.existing_id = existing_id


class PermissionDenied(BusinessError):
    """需要主管权限或负责人身份。"""


class TransferConflict(BusinessError):
    """并发转派竞争失败：承诺已被他人先转走。"""
