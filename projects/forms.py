from django import forms


class ProjectCreateForm(forms.Form):
    name = forms.CharField(label="Название проекта", max_length=100, strip=True)
