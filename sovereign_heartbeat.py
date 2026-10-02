from __future__ import annotations

import asyncio
import logging
import os
import sys

logger = logging.getLogger("sovereign_heartbeat")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - [HEARTBEAT] - %(message)s")


def _load_dependencies():
    from sqlalchemy import select  # type: ignore

    from dr_quinn_post_mortem import run_post_mortem_sweep  # type: ignore
    from friction_advanced_skill import (  # type: ignore
        FrictionEntryModel,
        SessionLocal,
        compute_priority_score,
        model_to_dict,
    )
    from mvs_engine import FrictionProfile, process_mvs_decision  # type: ignore
    from ralph5_master import ToolsmithRunRequest, orchestrate_toolsmith  # type: ignore

    return {
        "select": select,
        "run_post_mortem_sweep": run_post_mortem_sweep,
        "FrictionEntryModel": FrictionEntryModel,
        "SessionLocal": SessionLocal,
        "compute_priority_score": compute_priority_score,
        "model_to_dict": model_to_dict,
        "FrictionProfile": FrictionProfile,
        "process_mvs_decision": process_mvs_decision,
        "ToolsmithRunRequest": ToolsmithRunRequest,
        "orchestrate_toolsmith": orchestrate_toolsmith,
    }


def build_autonomy_loop(deps: dict):
    select = deps["select"]
    run_post_mortem_sweep = deps["run_post_mortem_sweep"]
    FrictionEntryModel = deps["FrictionEntryModel"]
    SessionLocal = deps["SessionLocal"]
    compute_priority_score = deps["compute_priority_score"]
    model_to_dict = deps["model_to_dict"]
    FrictionProfile = deps["FrictionProfile"]
    process_mvs_decision = deps["process_mvs_decision"]
    ToolsmithRunRequest = deps["ToolsmithRunRequest"]
    orchestrate_toolsmith = deps["orchestrate_toolsmith"]

    async def fetch_target():
        async with SessionLocal() as db:
            result = await db.execute(select(FrictionEntryModel).where(FrictionEntryModel.status == "unprocessed"))
            entries = result.scalars().all()
            if not entries:
                return None
            scored = [(compute_priority_score(model_to_dict(entry)), entry) for entry in entries]
            return scored[0][1]

    async def update_status(fid: int, status: str):
        async with SessionLocal() as db:
            entry = (await db.execute(select(FrictionEntryModel).where(FrictionEntryModel.id == fid))).scalars().first()
            if entry:
                entry.status = status
                await db.commit()

    async def autonomy_loop():
        logger.info("SOVEREIGN HEARTBEAT ONLINE. Scanning for friction...")
        while True:
            try:
                target = await fetch_target()
                if not target:
                    await asyncio.sleep(10)
                    continue

                fric_id = f"FRIC-{target.id}"
                logger.info("Target Acquired: %s", fric_id)
                profile = FrictionProfile(
                    friction_id=fric_id,
                    friction_point=target.friction_point,
                    root_cause=target.root_cause,
                    system_fix=target.system_fix,
                    impact=target.impact,
                    frequency=target.frequency,
                    time_wasted=target.time_wasted,
                )
                mvs_res = await asyncio.to_thread(process_mvs_decision, profile)
                if not mvs_res.approved:
                    logger.warning("MVS Rejected. Action: %s", mvs_res.routing_action)
                    await update_status(target.id, "rejected")
                    continue

                logger.info("MVS Approved. Engaging RALPH 5 Ultra...")
                req = ToolsmithRunRequest(
                    friction_id=fric_id,
                    friction_point=target.friction_point,
                    root_cause=target.root_cause,
                    system_fix=target.system_fix,
                )
                build_res = await asyncio.to_thread(orchestrate_toolsmith, req)
                await update_status(target.id, "promoted" if build_res.promoted else "failed")

                logger.info("Engaging Dr. Quinn Post-Mortem...")
                await asyncio.to_thread(run_post_mortem_sweep)
                logger.info("Cycle Complete.")

            except Exception as exc:  # pragma: no cover - runtime integration path
                logger.error("Heartbeat Error: %s", exc)
                await asyncio.sleep(10)

    return autonomy_loop


if __name__ == "__main__":
    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("Missing OPENAI_API_KEY")
    try:
        dependencies = _load_dependencies()
    except Exception as exc:
        sys.exit(f"Heartbeat dependency load failed: {exc}")
    asyncio.run(build_autonomy_loop(dependencies)())
