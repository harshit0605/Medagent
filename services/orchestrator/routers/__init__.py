"""Domain routers for the orchestrator HTTP API.

Each module here is a self-contained ``APIRouter`` extracted from the
historically-monolithic ``services/orchestrator/main.py``. ``main`` imports and
``include_router``s them. Routers depend only on repositories / shared services
(never on ``main``) to keep the import graph acyclic.
"""
