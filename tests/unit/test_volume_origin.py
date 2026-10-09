"""Un volumen huérfano indica de dónde salió (pila de CloudFormation o PVC de Kubernetes) para limpiar en el sitio correcto."""
from cloudcost.collectors.demo import DemoCollector
from cloudcost.domain.rules import RuleConfig, evaluate_all, rule_ebs_orphan


def _orphans():
    return [f for f in evaluate_all(DemoCollector().collect().resources, RuleConfig()) if f.rule_id == "ebs_orphan"]


def _volume(tags):
    r = next(x for x in DemoCollector().collect().resources if x.service == "ebs" and x.attached is False)
    r.tags = dict(tags)
    r.unattached_days = 60
    r.size_gb = max(r.size_gb or 0, 500)
    return r


def test_cloudformation_stack_is_named():
    f = rule_ebs_orphan(_volume({"aws:cloudformation:stack-name": "checkout-v2"}), RuleConfig())
    assert f.evidence["origin"] == "cloudformation" and f.evidence["origin_ref"] == "checkout-v2"
    assert "CloudFormation" in f.summary


def test_kubernetes_pvc_is_named():
    f = rule_ebs_orphan(_volume({"kubernetes.io/created-for/pvc/name": "data-0",
                                 "kubernetes.io/created-for/pvc/namespace": "orders"}), RuleConfig())
    assert f.evidence["origin"] == "kubernetes_pvc" and f.evidence["origin_ref"] == "orders/data-0"
    assert "kubectl" in f.summary


def test_without_origin_tags_nothing_is_added():
    f = rule_ebs_orphan(_volume({}), RuleConfig())
    assert "origin" not in f.evidence and "PersistentVolumeClaim" not in f.summary


def test_demo_k8s_leftovers_now_say_where_to_clean():
    assert any(f.evidence.get("origin") == "kubernetes_pvc" for f in _orphans())
