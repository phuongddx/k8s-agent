from k8s_agent import prompts


def test_investigate_prompt_says_exposure_can_come_from_an_ingress_or_an_ingressroute():
    assert (
        "Exposure can come from an Ingress or a Traefik IngressRoute; check both before concluding a service "
        "is not exposed, and never conclude 'not reachable' from one resource type alone."
    ) in prompts.INVESTIGATE


def test_runbook_5xx_row_covers_ingress_and_ingressroute():
    [row] = [r for r in prompts.RUNBOOK.splitlines() if r.startswith("| 5xx")]
    assert "Ingress or IngressRoute" in row
