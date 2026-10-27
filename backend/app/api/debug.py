"""Endpoints for testing single steps of the agent from /docs."""

from fastapi import APIRouter

from app.agent.planner import create_research_plan
from app.schemas import PageContent, ReadPageRequest, ResearchPlan, ResearchPlanRequest, SearchRequest, SearchResult
from app.services.search_service import get_search_service
from app.services.web_reader import get_web_reader

router = APIRouter(prefix="/api/debug", tags=["debug"])


@router.post("/plan", response_model=ResearchPlan)
async def plan_research(request: ResearchPlanRequest) -> ResearchPlan:
    return await create_research_plan(request.question)


@router.post("/search", response_model=list[SearchResult])
async def search(request: SearchRequest) -> list[SearchResult]:
    return await get_search_service().search(request.query, request.max_results)


@router.post("/read", response_model=PageContent)
async def read_page(request: ReadPageRequest) -> PageContent:
    return await get_web_reader().read(request.url)
