# worker/src/scripted/border_tax/__init__.py
"""Border tax scripted runners (one module per state).

Foundation branch ships the package skeleton + BorderTaxParams. The actual
state modules (up.py / hr.py / rj.py) come in their respective feature
branches. Each one must export:

    async def run(
        session,
        params: BorderTaxParams,
        log: StepLogger,
    ) -> RunOutcome
"""
