class StoreCorruption(ValueError):
    def __init__(self, path: str, offset: int, message: str) -> None:
        super().__init__(f"{path} at byte {offset}: {message}")
        self.path = path
        self.offset = offset
