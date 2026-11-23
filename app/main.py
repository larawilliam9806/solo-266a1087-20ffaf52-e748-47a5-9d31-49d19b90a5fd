"""FastAPI 入口：植物标本馆分类换版 API。"""
from __future__ import annotations

import os
from typing import List

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .schemas import IntakeBody, ReidentifyRequest, SpecimenBatch, SpecimenIn, SwitchRequest
from .service import ConflictError, HerbariumService, NotFoundError, ValidationError

DEFAULT_DB_PATH = os.environ.get("DATABASE_PATH", "./herbarium.db")
DEFAULT_INITIAL_VERSION = os.environ.get("INITIAL_VERSION", "1")


def create_app(db_path: str | None = None, initial_version: str | None = None) -> FastAPI:
    service = HerbariumService(
        db_path or DEFAULT_DB_PATH,
        initial_version or DEFAULT_INITIAL_VERSION,
    )

    app = FastAPI(
        title="植物标本馆分类换版 API",
        version="1.0.0",
        description="标本入库、分类换版核算、待重鉴名单与按种统计。",
    )
    app.state.service = service

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def _conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _invalid(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # ---------------------------------------------------------------- 健康/版本
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/version")
    def current_version() -> dict:
        return service.get_version()

    # ---------------------------------------------------------------- 入库
    @app.post("/api/specimens", status_code=201)
    def intake(body: IntakeBody = Body(...)) -> dict:
        """初次入库：提交标本号和种号。支持单件、{"items": [...]} 或数组。"""
        if isinstance(body, SpecimenIn):
            items: List[SpecimenIn] = [body]
        elif isinstance(body, SpecimenBatch):
            items = body.items
        else:
            items = list(body)
        return service.intake(items)

    # ---------------------------------------------------------------- 查询
    @app.get("/api/lists")
    def lists(version: str | None = Query(default=None)) -> dict:
        """两类名单（按种统计 + 待重鉴），附版号。"""
        return service.get_lists(version)

    @app.get("/api/counts")
    def counts(version: str | None = Query(default=None)) -> dict:
        data = service.get_lists(version)
        return {"version": data["version"], "counts": data["counts"]}

    @app.get("/api/pending")
    def pending(version: str | None = Query(default=None)) -> dict:
        data = service.get_lists(version)
        return {"version": data["version"], "pending": data["pending"]}

    @app.get("/api/specimens/{specimen_no}")
    def specimen(specimen_no: str, version: str | None = Query(default=None)) -> dict:
        return service.get_specimen(specimen_no, version)

    # ---------------------------------------------------------------- 换版
    @app.post("/api/version-switches", status_code=201)
    def switch_version(body: SwitchRequest) -> dict:
        """换版：注明现行版号，给每个旧种号填一个或多个新种号。"""
        return service.switch_version(
            body.current_version, body.new_version, body.mapping
        )

    # ---------------------------------------------------------------- 重鉴
    @app.post("/api/reidentifications", status_code=201)
    def reidentify(body: ReidentifyRequest) -> dict:
        """馆员凭当前版号，从待重鉴标本的候选中选定一种。"""
        return service.reidentify(body.version, body.specimen_no, body.species_no)

    return app


app = create_app()
