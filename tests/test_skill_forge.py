from app.skill_forge import evaluate_validation, skill_hash, validate_genome


def genome(**overrides):
    value = {
        'skill_id': 'skill.pocketos.test.runner',
        'version': '1.0.0',
        'name': 'Local Test Runner',
        'purpose': 'Run bounded repository tests.',
        'domain': 'Pocket OS',
        'author': 'operator',
        'permissions': {'read': True, 'write': False, 'network': False, 'filesystem': True, 'financial': False, 'exec_code': True},
        'governance': {'requires_approval': True, 'risk_level': 'medium', 'sail_approved': True},
    }
    value.update(overrides)
    return value


def report(**overrides):
    value = {'passed': True, 'functional_score': 96, 'security_score': 95, 'governance_score': 94, 'compliance_score': 94, 'security_issues': []}
    value.update(overrides)
    return value


def test_skill_identity_is_deterministic():
    assert skill_hash(genome()) == skill_hash(dict(reversed(list(genome().items()))))


def test_validation_fails_closed_without_sail_approval():
    decision = evaluate_validation(genome(governance={'requires_approval': True, 'risk_level': 'medium', 'sail_approved': False}), report())
    assert decision.status == 'testing'
    assert decision.passed is False
    assert decision.reason == 'SAIL_APPROVAL_UNVERIFIED'


def test_validation_rejects_security_issues():
    decision = evaluate_validation(genome(), report(security_issues=[{'severity': 'high', 'title': 'hidden call'}]))
    assert decision.status == 'rejected'
    assert decision.passed is False


def test_validation_approves_only_complete_evidence():
    decision = evaluate_validation(genome(), report())
    assert decision.status == 'approved'
    assert decision.passed is True


def test_invalid_genome_is_rejected():
    errors = validate_genome({'skill_id': 'broken'})
    assert 'MISSING_GENOME_FIELD:version' in errors
