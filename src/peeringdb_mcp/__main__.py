import uvicorn


def main() -> None:
    uvicorn.run(
        "peeringdb_mcp.server:create_app",
        factory=True,
        host="127.0.0.1",
        port=8001,
        workers=1,
        reload=False,
    )


if __name__ == "__main__":
    main()
