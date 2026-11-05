"""One APIRouter per area, all mounted under /api by create_app (paths below are relative to /api)."""

from seedbox2.server.routers import analysis, assistant, events, jobs, library, listen, mixes, models, onboarding, power, sets, system, tracks

ROUTERS = [system.router, events.router, library.router, analysis.router, sets.router, mixes.router, jobs.router,
           tracks.router, models.router, listen.router,
           onboarding.router, power.router, assistant.router]
