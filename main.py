from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .configurations.session import  create_all_tables, insert_data, create_database_if_not_exists                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     

from .config import settings
from .routes import app_router as all_routes
from common.observability_utils.bootstrap import bootstrap
from common.observability_utils.instrumentation import instrument_fastapi

logger, tracer, meter = bootstrap(settings.logging.name, settings.logging.uri)



@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting FastAPI application...")
    await create_database_if_not_exists()
    await create_all_tables()
    await insert_data()

    # logger.info("Tables created successfully")
    try:
        yield
    finally:
        logger.info("Application shutting down")


app = FastAPI(title="CRUD API", lifespan=lifespan)
app.include_router(all_routes, prefix="/crud")
instrument_fastapi(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.server.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.server.port)
