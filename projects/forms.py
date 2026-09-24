from django import forms


class ProjectCreateForm(forms.Form):
    name = forms.CharField(label="Название проекта", max_length=100, strip=True)


class ProjectInputsForm(forms.Form):
    def __init__(self, *args, profile, **kwargs):
        super().__init__(*args, **kwargs)
        for number, field in enumerate(profile["fields"]):
            if field["kind"] == "boolean":
                self.fields[f"field_{number}"] = forms.ChoiceField(
                    label=field["label"], required=False,
                    choices=(("", "Нет данных"), ("true", "Да"), ("false", "Нет")),
                    initial="true" if field["value"] is True else "false" if field["value"] is False else "",
                )
            else:
                self.fields[f"field_{number}"] = forms.CharField(
                    label=field["label"], max_length=200, required=False,
                    initial=field["value"],
                )
