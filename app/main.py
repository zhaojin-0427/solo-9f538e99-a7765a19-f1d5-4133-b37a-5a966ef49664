"""FastAPI application: migration control, progress, and metric queries."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, Path, Query, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .issuance import (
    MAX_BATCH_SAMPLES,
    BatchNotFound,
    IssuanceRejected,
    IssuanceService,
    IssueBatchIn,
)
from .migration import MigrationEngine, MigrationError
from .repository import (
    DEFAULT_PAGE,
    MAX_PAGE,
    InvalidQuery,
    SampleNotFound,
    SampleRepository,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = MigrationEngine(settings)
    repository = SampleRepository(settings)
    issuance = IssuanceService(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine.initialize()
        app.state.settings = settings
        app.state.engine = engine
        app.state.repository = repository
        app.state.issuance = issuance
        yield
        engine.wait_for_idle(timeout=5)

    app = FastAPI(
        title="Lab wide-to-long migration API",
        version="1.1.0",
        description=(
            "在线将 samples(id, temperature, ph) 宽表迁移为 sample_metrics 长表；"
            "迁移期间旧采集程序仍可读写，切换后 samples 由兼容视图提供。"
            "另提供质控签发：按闭区间核对至多 1000 个互异样本，全部合格才生成"
            "不可变签发批次，并可分页读回签发时的原值。"
        ),
        lifespan=lifespan,
    )

    def get_engine(request: Request) -> MigrationEngine:
        return request.app.state.engine

    def get_repo(request: Request) -> SampleRepository:
        return request.app.state.repository

    def get_issuance(request: Request) -> IssuanceService:
        return request.app.state.issuance

    @app.exception_handler(InvalidQuery)
    async def _invalid_query(_request: Request, exc: InvalidQuery) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(SampleNotFound)
    async def _not_found(_request: Request, exc: SampleNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(BatchNotFound)
    async def _batch_not_found(
        _request: Request, exc: BatchNotFound
    ) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(IssuanceRejected)
    async def _issuance_rejected(
        _request: Request, exc: IssuanceRejected
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "issuance rejected; no batch was created",
                "sample_count": len(exc.errors),
                "errors": exc.errors,
            },
        )

    @app.exception_handler(MigrationError)
    async def _migration_error(_request: Request, exc: MigrationError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    # ------------------------------------------------------------- migration
    @app.post("/migrations", status_code=202)
    def start_migration(request: Request) -> dict[str, Any]:
        """发起迁移。

        幂等：已完成 → `action=reused`（不重建）；运行中 → 返回当前进度；
        失败 → 从已提交的检查点续跑；从未开始 → 新建并启动后台复制。
        """
        state, started = get_engine(request).start()
        return {
            "action": "started" if started else "reused",
            "migration": state,
            "progress_url": "/migrations/current",
        }

    @app.get("/migrations/current")
    def migration_status(request: Request) -> dict[str, Any]:
        """查询迁移进度与状态（not_started / copying / completed / failed）。"""
        engine = get_engine(request)
        state = engine.get_state()
        # The persisted last_error (if any) is authoritative across restarts;
        # the in-memory one only covers a failure earlier in this process.
        return {
            "migration": state,
            "last_error": state.get("last_error") or engine.last_error,
        }

    @app.get("/migrations/current/events")
    def migration_events(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> dict[str, Any]:
        return {"events": get_engine(request).events(limit=limit)}

    # --------------------------------------------------------------- metrics
    @app.get("/metrics/{metric}")
    def list_metric(
        request: Request,
        metric: str,
        after_id: Annotated[int | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    ) -> dict[str, Any]:
        """按指标装载长表（键集分页，按 sample_id 递增）。"""
        return get_repo(request).list_metric(metric, after_id=after_id, limit=limit)

    @app.get("/samples/{sample_id}/metrics")
    def sample_metrics(
        request: Request,
        sample_id: Annotated[int, Path()],
    ) -> dict[str, Any]:
        """按样本查询各指标当前值；切换后即长表内容，缺失返回 404。"""
        return get_repo(request).get_metrics(sample_id)

    @app.get("/samples")
    def list_samples(
        request: Request,
        after_id: Annotated[int | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    ) -> dict[str, Any]:
        """兼容视图读取（与旧程序看到的 samples 完全一致）。"""
        return get_repo(request).list_samples(after_id=after_id, limit=limit)

    @app.get("/samples/{sample_id}")
    def get_sample(
        request: Request,
        sample_id: Annotated[int, Path()],
    ) -> dict[str, Any]:
        """兼容视图单行读取（旧程序 SELECT 的等价入口）。"""
        return get_repo(request).get_sample(sample_id)

    @app.get("/metrics", include_in_schema=True)
    def metrics_index() -> dict[str, Any]:
        """Available metrics and their query entry points."""
        temperature = {"metric": "temperature", "url": "/metrics/temperature"}
        ph = {"metric": "ph", "url": "/metrics/ph"}
        return {
            "metrics": [temperature, ph],
            "sample_url": "/samples/{sample_id}/metrics",
        }

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, Any]:
        state = get_engine(request).get_state()
        return {
            "status": "ok",
            "migration_state": state.get("state"),
            "view_installed": state.get("view_installed", False),
        }

    # -------------------------------------------------------------- issuance
    @app.post("/issuances", status_code=201)
    def create_issuance(request: Request, body: IssueBatchIn) -> dict[str, Any]:
        """签发一批观测。

        请求包含至多 1000 个互异样本 ID 和 temperature / ph 两个闭区间。
        按一个已提交状态检查每个样本存在、两项数值非空且落在区间内；有任何
        问题返回 422 和逐样本原因，**不生成批次**。全部合格才保存不可变批次
        （含所用区间与签发时的原值）。核查期间不持有写锁，旧采集程序的更正可
        继续提交；提交前会在短的写事务内用最新已提交状态复核。
        """
        return {
            "action": "issued",
            "batch": get_issuance(request).issue(body),
        }

    @app.get("/issuances")
    def list_issuances(
        request: Request,
        after_batch_id: Annotated[int | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    ) -> dict[str, Any]:
        """按批次 ID 分页列出已签发批次（不含逐样本原值）。"""
        return get_issuance(request).list_batches(
            after_batch_id=after_batch_id, limit=limit
        )

    @app.get("/issuances/max-samples")
    def issuance_limits() -> dict[str, int]:
        """The issuance request limit (useful in /docs)."""
        return {"max_samples_per_batch": MAX_BATCH_SAMPLES}

    @app.get("/issuances/{batch_id}")
    def get_issuance_batch(
        request: Request, batch_id: Annotated[int, Path()]
    ) -> dict[str, Any]:
        """批次摘要与区间。"""
        return get_issuance(request).get_batch(batch_id)

    @app.get("/issuances/{batch_id}/items")
    def get_issuance_items(
        request: Request,
        batch_id: Annotated[int, Path()],
        after_sample_id: Annotated[int | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    ) -> dict[str, Any]:
        """分页读取签发时的原值（按 sample_id 递增，键集分页）。

        原值来自签发时快照；之后旧程序的更正不会改变这里的内容。
        """
        return get_issuance(request).list_batch_items(
            batch_id, after_sample_id=after_sample_id, limit=limit
        )

    return app


app = create_app()
