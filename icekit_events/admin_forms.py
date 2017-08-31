"""
Admin forms for ``icekit_events`` app.
"""

from django import forms

from . import models


class BaseEventRepeatsGeneratorForm(forms.ModelForm):

    class Meta:
        fields = '__all__'
        model = models.EventRepeatsGenerator

    @property
    def media(self):
        media = super(BaseEventRepeatsGeneratorForm, self).media
        media.add_js(['admin/js/event_all_day_field.js'])
        return media

    def clean(self):
        cleaned_data = super(BaseEventRepeatsGeneratorForm, self).clean()
        # Handle situation where hidden time fields in admin UI submit the
        # value "00:00:00" for `repeat_end` without a corresponding date.
        if cleaned_data['repeat_end'] and not cleaned_data['recurrence_rule']:
            self.add_error('recurrence_rule', 'Recurrence rule must be set if a repeat end date/time is')
        if 'repeat_end' in self.errors:
            if self.data.get(self.prefix + '-repeat_end_1') == '00:00:00' \
                    and not self.data.get(self.prefix + '-repeat_end_0'):
                cleaned_data['repeat_end'] = None
                del(self.errors['repeat_end'])
        return cleaned_data


class BaseOccurrenceForm(forms.ModelForm):

    class Meta:
        fields = '__all__'
        model = models.Occurrence

    @property
    def media(self):
        media = super(BaseOccurrenceForm, self).media
        media.add_js(['admin/js/event_all_day_field.js'])
        return media

    def save(self, *args, **kwargs):
        # We assume any change to a model made via the admin (as opposed to
        # an ``EventRepeatsGenerator``) implies a user modification.
        occurrence = self.instance
        occurrence._flag_user_modification = True
        return super(BaseOccurrenceForm, self).save(*args, **kwargs)


# For some reason, BaseEventForm doesn't play well with AnyURLField before
# migrations have been applied. Since all it does is resize text widgets,
# leaving it commented for now.
# IC Slack: https://theicteam.slack.com/files/jamesmurty/F2Q2S4S8N/vexing_contenttypes_issue_enabling_icekit_events.txt
# TODO: figure out why

# DEFAULT_EVENT_FORM_WIDGETS = {
#     'human_dates': forms.Textarea({'cols': 80, 'rows': 3}),
#     'special_instructions': forms.Textarea({'cols': 80, 'rows': 4}),
# }
#
# class BaseEventForm(forms.ModelForm):
#
#     class Meta:
#         fields = '__all__'
#         model = models.EventBase
#         widgets = DEFAULT_EVENT_FORM_WIDGETS
