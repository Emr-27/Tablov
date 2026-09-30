class ContractError(Exception):
    """输入、配置或本阶段不支持的音乐语义。"""

    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")

