import uvicorn

if __name__ == "__main__":
    uvicorn.run("services.hub.app:app", host="0.0.0.0", port=8080, reload=False)

