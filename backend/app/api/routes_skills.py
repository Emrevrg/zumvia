from fastapi import APIRouter

from ..layers.playbooks import PLAYBOOKS, playbook_catalog
from ..layers.skills import SKILLS, count_by_category, skill_catalog

router = APIRouter(prefix="/api", tags=["Katalog"])

@router.get("/skills")
def skills():
    return {"skills": skill_catalog(), "count": len(SKILLS), "by_category": count_by_category()}

@router.get("/catalog")
def catalog():
    return {
        "playbooks": playbook_catalog(),
        "skills": skill_catalog(),
        "counts": {"playbooks": len(PLAYBOOKS), "skills": len(SKILLS), "by_category": count_by_category()},
    }
