"""One APIRouter per area, all mounted under /api by create_app (paths below are relative to /api)."""

from seedbox2.server.routers import analysis, events, jobs, library, mixes, models, sets, system, tracks

ROUTERS = [system.router, events.router, library.router, analysis.router, sets.router, mixes.router, jobs.router,
           tracks.router, models.router]
