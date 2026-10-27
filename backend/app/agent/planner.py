from app.agent.prompts import PLANNER_INSTRUCTION, build_planner_prompt
from app.schemas import ResearchPlan
from app.services.llm_service import get_llm_service


async def create_research_plan(question: str) -> ResearchPlan:
    return await get_llm_service().generate_structured(
        build_planner_prompt(question),
        ResearchPlan,
        system_instruction=PLANNER_INSTRUCTION,
    )
