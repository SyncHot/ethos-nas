"""Video metadata providers (keyless Cinemeta/TVmaze + optional TMDB).

The package is deliberately independent of FastAPI routers: providers do HTTP,
normalize their payloads into `MetadataMatch`, and `service.py` owns the
provider chain, caching and image lifecycle. The router is a thin API layer.
"""
