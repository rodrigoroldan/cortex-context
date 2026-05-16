if __name__ == "__main__":
    import uvicorn
    from app.config import get_settings
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.cortex_host,
        port=settings.cortex_port,
        reload=settings.app_env == "development",
        log_level=settings.log_level.lower(),
    )
