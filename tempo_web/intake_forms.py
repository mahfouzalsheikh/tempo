from django import forms
from django.forms import formset_factory

from .models import Project


def prose(label, *, required=True):
    return forms.CharField(
        label=label, required=required, max_length=4000, widget=forms.Textarea(attrs={"rows": 3})
    )


class BriefForm(forms.Form):
    project = forms.ModelChoiceField(queryset=Project.objects.filter(active=True))
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    expected_revision = forms.IntegerField(required=False, widget=forms.HiddenInput)
    title = forms.CharField(max_length=200, label="What are you building?")
    goal = prose("What problem should it solve?")
    users = prose("Who will use it?")
    scope = prose("What must this version include?")
    exclusions = prose("What is outside this version?", required=False)
    constraints = prose("Technical, time, or budget constraints", required=False)
    target = prose("Where should it run?")
    open_questions = prose("Questions to resolve before approving the plan", required=False)


class CriterionForm(forms.Form):
    criterion_id = forms.CharField(required=False, max_length=50, widget=forms.HiddenInput)
    outcome = prose("Expected behavior")
    verification = prose("How will we verify it?")


CriterionFormSet = formset_factory(
    CriterionForm,
    extra=0,
    min_num=1,
    validate_min=True,
    max_num=30,
    validate_max=True,
    absolute_max=40,
    can_delete=True,
)


class TaskForm(forms.Form):
    id = forms.CharField(label="Task ID", max_length=50)
    title = forms.CharField(label="Task title", max_length=200)
    role = forms.ChoiceField(
        choices=[
            ("planner", "Planning"),
            ("implementer", "Implementation"),
            ("integrator", "Integration"),
            ("verifier", "Independent verification"),
        ]
    )
    instructions = prose("Deliverables and instructions")
    repository_write = forms.BooleanField(
        label="This task needs to edit repository files", required=False,
    )
    required_files = forms.CharField(
        label="Required committed files (one repository-relative path per line)",
        required=False, max_length=12000, widget=forms.Textarea(attrs={"rows": 2}),
    )
    required_decisions = forms.CharField(
        label="Required decision IDs (comma separated)", required=False, max_length=2000,
        help_text="The agent must return a decision and rationale for each ID.",
    )
    criteria = forms.CharField(label="Acceptance criterion IDs (comma separated)", required=False)
    depends_on = forms.CharField(label="Prerequisite task IDs (comma separated)", required=False)


TaskFormSet = formset_factory(
    TaskForm,
    extra=0,
    min_num=1,
    validate_min=True,
    max_num=50,
    validate_max=True,
    absolute_max=60,
    can_delete=True,
)
