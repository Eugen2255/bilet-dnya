class ImportProblem(Exception):
    def __init__(self, code: str, message: str, *, filename: str | None = None, page: int | None = None):
        super().__init__(message)
        self.code = code
        self.filename = filename
        self.page = page

    def detail(self) -> dict:
        info = {"code": self.code, "message": str(self)}
        if self.filename is not None:
            info["filename"] = self.filename
        if self.page is not None:
            info["page"] = self.page
        return info
