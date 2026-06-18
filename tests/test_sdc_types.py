from r6.models import R6Resource
from r6.validator import R6_RESOURCE_TYPES


def test_questionnaire_types_are_supported():
    assert R6Resource.is_supported_type('Questionnaire')
    assert R6Resource.is_supported_type('QuestionnaireResponse')


def test_questionnaire_types_in_validator_list():
    assert 'Questionnaire' in R6_RESOURCE_TYPES
    assert 'QuestionnaireResponse' in R6_RESOURCE_TYPES
