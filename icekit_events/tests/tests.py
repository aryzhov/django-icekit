# -*- encoding: utf8 -*-
"""
Tests for ``icekit_events`` app.
"""

# WebTest API docs: http://webtest.readthedocs.org/en/latest/api.html
from unittest import skip

from icekit_events.admin_forms import BaseEventRepeatsGeneratorForm
from timezone import timezone as djtz  # django-timezone
from datetime import datetime, timedelta, time
import six
import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.urlresolvers import reverse
from django.forms.models import fields_for_model
from django.test import TestCase
from django.test.utils import override_settings

from django_dynamic_fixture import G
from django_webtest import WebTest

from icekit import models as icekit_models
from icekit_events import appsettings, forms, models
from icekit_events.event_types.simple.models import SimpleEvent
from icekit_events.models import get_occurrence_times_for_event, coerce_naive, \
    Occurrence, RecurrenceRule
from icekit_events.utils.timeutils import localize_preserving_time_of_day
from icekit_events.utils import timeutils


class TestAdmin(WebTest):

    def setUp(self):
        self.User = get_user_model()
        self.superuser = G(
            self.User,
            is_staff=True,
            is_superuser=True,
        )
        self.superuser.set_password('abc123')
        self.start = timeutils.round_datetime(
            when=djtz.now(),
            precision=timedelta(minutes=1),
            rounding=timeutils.ROUND_DOWN)
        self.end = self.start + timedelta(minutes=45)
        self.layout = icekit_models.Layout.auto_add(
            'icekit_event_types_simple/layouts/default.html',
            SimpleEvent,
        )
        # Make sure default recurrence rules exist
        # TODO I'm not sure why this is necessary in unit tests, but something
        # is blowing away RecurrenceRule entries during test runs so we do this
        # to replace the defaults if necessary.
        # RULES are from *icekit_events/migrations/0002_recurrence_rules.py*
        RULES = [
            ('Daily, except Xmas day', 'RRULE:FREQ=DAILY;\nEXRULE:FREQ=YEARLY;BYMONTH=12;BYMONTHDAY=25'),
            ('Daily, Weekdays, except Xmas day', 'RRULE:FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;\nEXRULE:FREQ=YEARLY;BYMONTH=12;BYMONTHDAY=25'),
            ('Daily, Weekends, except Xmas day', 'RRULE:FREQ=DAILY;BYDAY=SA,SU;\nEXRULE:FREQ=YEARLY;BYMONTH=12;BYMONTHDAY=25'),
            ('Weekly, except Xmas day', 'RRULE:FREQ=WEEKLY;\nEXRULE:FREQ=YEARLY;BYMONTH=12;BYMONTHDAY=25'),
            ('Monthly, except Xmas day', 'RRULE:FREQ=MONTHLY;\nEXRULE:FREQ=YEARLY;BYMONTH=12;BYMONTHDAY=25'),
            ('Yearly, except Xmas day', 'RRULE:FREQ=YEARLY;\nEXRULE:FREQ=YEARLY;BYMONTH=12;BYMONTHDAY=25'),
        ]
        for description, recurrence_rule in RULES:
            RecurrenceRule.objects.get_or_create(
                description=description,
                defaults=dict(recurrence_rule=recurrence_rule),
            )
        self.daily_recurrence_rule = RecurrenceRule.objects.get(
            description='Daily, except Xmas day')
        self.weekend_days_recurrence_rule = RecurrenceRule.objects.get(
            description='Daily, Weekends, except Xmas day')
        self.weekly_recurrence_rule = RecurrenceRule.objects.get(
            description='Weekly, except Xmas day')

    def test_urls(self):
        response = self.app.get(
            reverse('admin:icekit_events_recurrencerule_changelist'),
            user=self.superuser,
        )
        self.assertEqual(200, response.status_code)

    def test_admin_repeats_generator_form(self):
        # check the validation on the form
        current = djtz.now()
        one_day = timedelta(days=1)
        past = current - one_day
        future = current + one_day
        recurrence_rule_list = [
            self.daily_recurrence_rule.pk,
            'every day',
            'RRULE:FREQ=DAILY',
            ]
        # check we can submit a valid form
        form = BaseEventRepeatsGeneratorForm(
            {
                'start': current,
                'end': future,
                'is_all_day': False,
                'repeat_end': future,
                'event': 1,
                'recurrence_rule': recurrence_rule_list,
            }
        )
        self.assertTrue(form.is_valid())

        # if you provide a start, you must also provide an end
        form = BaseEventRepeatsGeneratorForm(
            {
                'start': current,
                'is_all_day': False,
                'repeat_end': future,
                'event': 1,
                'recurrence_rule': recurrence_rule_list,
            }
        )
        self.assertFalse(form.is_valid())

        # end must be same as or after start
        form = BaseEventRepeatsGeneratorForm(
            {
                'start': current,
                'end': past,
                'is_all_day': False,
                'repeat_end': future,
                'event': 1,
                'recurrence_rule': recurrence_rule_list,
            }
        )
        self.assertFalse(form.is_valid())

        # if you supply repeat_end, you must supply a recurrence rule
        form = BaseEventRepeatsGeneratorForm(
            {
                'start': current,
                'end': future,
                'is_all_day': False,
                'repeat_end': future,
                'event': 1,
                'recurrence_rule': None,
            }
        )
        self.assertFalse(form.is_valid())

        # repeat_end cannot be before start
        form = BaseEventRepeatsGeneratorForm(
            {
                'start': current,
                'end': future,
                'is_all_day': False,
                'repeat_end': past,
                'event': 1,
                'recurrence_rule': recurrence_rule_list,
            }
        )
        self.assertFalse(form.is_valid())

    def test_create_event(self):
        # Load admin Add page, which lists polymorphic event child models
        response = self.app.get(
            reverse('admin:icekit_events_eventbase_add'),
            user=self.superuser,
        )
        # If there are multiple polymorphic child event choices, choose Event
        # type from these choices (no choices are presented unless there are
        # multiple polymorphic child types)
        if response.status_code == 302:
            # Single polymorphic child type, so we immediately get redirected
            response = response.follow()
        else:
            form = response.forms[0]
            ct_id = ContentType.objects.get_for_model(models.EventBase).pk
            form['ct_id'].select(ct_id)
            response = form.submit().follow()  # Follow to get "?ct_id=" param
        # Fill in and submit actual Event admin add form
        form = response.forms[0]
        form['title'].value = u"Test Event"
        form['slug'].value = 'test-event'
        response = form.submit()
        self.assertEqual(302, response.status_code)
        response = response.follow()
        event = SimpleEvent.objects.get(title=u"Test Event")
        self.assertEqual(0, event.repeat_generators.count())
        self.assertEqual(0, event.occurrences.count())

    def test_event_with_eventrepeatsgenerators(self):
        event = G(
            SimpleEvent,
            title='Test Event',
            layout=self.layout,
        )
        response = self.app.get(
            reverse('admin:icekit_events_eventbase_change', args=(event.pk,)),
            user=self.superuser,
        )
        #######################################################################
        # Add "Daily" repeat generator, spans 1 week
        #######################################################################
        repeat_end = localize_preserving_time_of_day(
            self.start + timedelta(days=7))
        form = response.forms[0]
        form['repeat_generators-0-recurrence_rule_0'].select(
            str(self.daily_recurrence_rule.pk))
        form['repeat_generators-0-recurrence_rule_1'].value = 'every day'
        form['repeat_generators-0-recurrence_rule_2'].value = "RRULE:FREQ=DAILY"
        form['repeat_generators-0-start_0'].value = \
            self.start.strftime('%Y-%m-%d')
        form['repeat_generators-0-start_1'].value = \
            self.start.strftime('%H:%M:%S')
        form['repeat_generators-0-end_0'].value = \
            self.end.strftime('%Y-%m-%d')
        form['repeat_generators-0-end_1'].value = \
            self.end.strftime('%H:%M:%S')
        form['repeat_generators-0-repeat_end_0'].value = \
            repeat_end.strftime('%Y-%m-%d')
        form['repeat_generators-0-repeat_end_1'].value = \
            repeat_end.strftime('%H:%M:%S')
        response = form.submit(name='_continue')
        # Check occurrences created
        event = SimpleEvent.objects.get(pk=event.pk)
        self.assertEqual(1, event.repeat_generators.count())
        self.assertEqual(7, event.occurrences.count())
        self.assertEqual(
            self.start, event.occurrences.all()[0].start)
        self.assertEqual(
            self.end, event.occurrences.all()[0].end)
        self.assertEqual(
            localize_preserving_time_of_day(self.start + timedelta(days=6)),
            event.occurrences.all()[6].start)
        self.assertEqual(
            localize_preserving_time_of_day(self.end + timedelta(days=6)),
            event.occurrences.all()[6].end)
        #######################################################################
        # Add "Daily on weekends" all-day repeat generator, no repeat end
        #######################################################################
        form = response.follow().forms[0]
        form['repeat_generators-1-recurrence_rule_0'].select(
            str(self.weekend_days_recurrence_rule.pk))
        form['repeat_generators-1-recurrence_rule_1'].value = \
            'every day on Saturday, Sunday'
        form['repeat_generators-1-recurrence_rule_2'].value = \
            "RRULE:FREQ=DAILY;BYDAY=SA,SU"
        form['repeat_generators-1-is_all_day'].value = True
        form['repeat_generators-1-start_0'].value = \
            self.start.strftime('%Y-%m-%d')
        form['repeat_generators-1-start_1'].value = '00:00:00'
        form['repeat_generators-1-end_0'].value = \
            self.start.strftime('%Y-%m-%d')  # NOTE: end date == start date
        form['repeat_generators-1-end_1'].value = '00:00:00'
        response = form.submit('_continue')
        # Check occurrences created
        event = SimpleEvent.objects.get(pk=event.pk)
        daily_generator = event.repeat_generators.all()[0]
        daily_wend_generator = event.repeat_generators.all()[1]
        daily_occurrences = event.occurrences.filter(generator=daily_generator)
        daily_wend_occurrences = event.occurrences.filter(
            generator=daily_wend_generator)
        self.assertEqual(2, event.repeat_generators.count())
        self.assertEqual(7, daily_occurrences.count())
        # app_settings.REPEAT_LIMIT = 13 weeks
        # when run on some days the next 13 weeks contains 1 more weekend day
        self.assertTrue(
            daily_wend_occurrences.count() in [13 * 2, 13 * 2 + 1])
        today = djtz.now().date()
        if today.weekday() not in [5, 6]: # if this is run on a weekend, the next generated occurrences differ
            self.assertEqual(
                5,  # Saturday
                coerce_naive(daily_wend_occurrences[0].start).weekday())
            self.assertEqual(
                6,  # Sunday
                coerce_naive(daily_wend_occurrences[1].start).weekday())
        # Start and end dates of all-day occurrences are zeroed
        self.assertEqual(
            time(0, 0),
            daily_wend_occurrences[0].start.astimezone(djtz.get_current_timezone()).time())
        self.assertEqual(
            time(0, 0),
            daily_wend_occurrences[0].end.astimezone(djtz.get_current_timezone()).time())
        #######################################################################
        # Delete "Daily" repeat generator
        #######################################################################
        form = response.follow().forms[0]
        form['repeat_generators-0-DELETE'].value = True
        response = form.submit('_continue')
        event = SimpleEvent.objects.get(pk=event.pk)
        self.assertEqual(1, event.repeat_generators.count())
        self.assertEqual(daily_wend_occurrences.count(), event.occurrences.count())

    def test_event_with_protected_occurrences(self):
        event = G(
            SimpleEvent,
            title='Test Event',
            layout=self.layout,
        )
        response = self.app.get(
            reverse('admin:icekit_events_eventbase_change', args=(event.pk,)),
            user=self.superuser,
        )
        self.assertEqual(0, event.occurrences.count())
        #######################################################################
        # Add timed occurrence manually
        #######################################################################
        form = response.forms[0]
        form['occurrences-0-start'].value = \
            self.start.strftime('%Y-%m-%d %H:%M:%S')
        form['occurrences-0-end'].value = \
            self.end.strftime('%Y-%m-%d %H:%M:%S')
        response = form.submit('_continue')
        self.assertEqual(1, event.occurrences.count())
        timed_occurrence = event.occurrences.all()[0]
        self.assertTrue(timed_occurrence.is_protected_from_regeneration)
        self.assertEqual(
            self.start, timed_occurrence.start)
        self.assertEqual(
            self.end, timed_occurrence.end)
        #######################################################################
        # Add all-day occurrence manually
        #######################################################################
        form = response.follow().forms[0]
        all_day_start = localize_preserving_time_of_day(
            self.start + timedelta(days=3))
        form['occurrences-1-start'].value = \
            all_day_start.strftime('%Y-%m-%d 00:00:00')
        form['occurrences-1-end'].value = \
            all_day_start.strftime('%Y-%m-%d 00:00:00')
        form['occurrences-1-is_all_day'].value = True
        response = form.submit('_continue')
        event = SimpleEvent.objects.get(pk=event.pk)
        self.assertEqual(2, event.occurrences.count())
        all_day_occurrence = event.occurrences.all()[1]
        self.assertTrue(timed_occurrence.is_protected_from_regeneration)
        # Start and end dates of all-day occurrences are zeroed
        self.assertEqual(
            time(0, 0),
            all_day_occurrence.start.astimezone(
                djtz.get_current_timezone()).time())
        self.assertEqual(
            time(0, 0),
            all_day_occurrence.end.astimezone(
                djtz.get_current_timezone()).time())
        # Commenting out this test as the corresponding admin field is currently
        # excluded.
        # #######################################################################
        # # Cancel first (timed) event
        # #######################################################################
        # form = response.follow().forms[0]
        # form['occurrences-0-cancel_reason'].value = 'Sold out'
        # response = form.submit('_continue')
        # self.assertEqual(2, event.occurrences.count())
        # timed_occurrence = event.occurrences.all()[0]
        # self.assertEqual('Sold out', timed_occurrence.cancel_reason)
        # self.assertTrue(timed_occurrence.is_cancelled)
        #######################################################################
        # Delete second (all-day) event
        #######################################################################
        form = response.follow().forms[0]
        form['occurrences-1-DELETE'].value = True
        response = form.submit('_continue')
        self.assertEqual(1, event.occurrences.count())

    def test_event_with_repeatsgenerators_and_protected_occurrences(self):
        event = G(
            SimpleEvent,
            title='Test Event',
            layout=self.layout,
        )
        # Generate a repeat end date 10 weeks ahead
        repeat_end = localize_preserving_time_of_day(
            self.start + timedelta(weeks=10))
        G(
            models.EventRepeatsGenerator,
            event=event,
            start=self.start,
            end=self.end,
            recurrence_rule='FREQ=WEEKLY',
            repeat_end=repeat_end,
        )
        self.assertEqual(10, event.occurrences.count())
        self.assertEqual(10, event.occurrences.generated().count())
        response = self.app.get(
            reverse('admin:icekit_events_eventbase_change', args=(event.pk,)),
            user=self.superuser,
        )
        first_occurrence = event.occurrences.all()[0]
        #######################################################################
        # Add occurrence manually
        #######################################################################
        form = response.forms[0]
        extra_occurrence_start = localize_preserving_time_of_day(
            first_occurrence.start - timedelta(days=3))
        extra_occurrence_end = localize_preserving_time_of_day(
            first_occurrence.end - timedelta(days=3))
        form['occurrences-10-start'].value = \
            extra_occurrence_start.strftime('%Y-%m-%d %H:%M:%S')
        form['occurrences-10-end'].value = \
            extra_occurrence_end.strftime('%Y-%m-%d %H:%M:%S')
        response = form.submit('_continue')
        self.assertEqual(10 + 1, event.occurrences.count())
        extra_occurrence = event.occurrences.all()[0]
        self.assertTrue(extra_occurrence.is_protected_from_regeneration)
        self.assertFalse(extra_occurrence.is_generated)
        self.assertEqual(
            extra_occurrence_start, extra_occurrence.start)
        self.assertEqual(
            extra_occurrence_end, extra_occurrence.end)
        #######################################################################
        # Adjust start time of a generated occurrence
        #######################################################################
        form = response.follow().forms[0]
        shifted_occurrence = event.occurrences.all()[6]
        self.assertFalse(shifted_occurrence.is_protected_from_regeneration)
        shifted_occurrence_start = \
            (shifted_occurrence.start + timedelta(minutes=30)) \
            .astimezone(djtz.get_current_timezone())
        form['occurrences-6-start'].value = \
            shifted_occurrence_start.strftime('%Y-%m-%d %H:%M:%S')
        response = form.submit('_continue')
        event = SimpleEvent.objects.get(pk=event.pk)
        self.assertEqual(10 + 1, event.occurrences.count())
        shifted_occurrence = models.Occurrence.objects.get(
            pk=shifted_occurrence.pk)
        self.assertTrue(shifted_occurrence.is_protected_from_regeneration)
        self.assertTrue(shifted_occurrence.is_generated)
        self.assertEqual(
            shifted_occurrence_start, shifted_occurrence.start)
        self.assertEqual(
            shifted_occurrence_start + timedelta(minutes=15),
            shifted_occurrence.end)
        #######################################################################
        # Convert a timed generated occurrence to all-day
        #######################################################################
        form = response.follow().forms[0]
        converted_occurrence = event.occurrences.all()[2]
        self.assertFalse(converted_occurrence.is_protected_from_regeneration)
        form['occurrences-2-is_all_day'].value = True
        response = form.submit('_continue')
        event = SimpleEvent.objects.get(pk=event.pk)
        self.assertEqual(10 + 1, event.occurrences.count())
        converted_occurrence = models.Occurrence.objects.get(
            pk=converted_occurrence.pk)
        self.assertTrue(converted_occurrence.is_protected_from_regeneration)
        self.assertTrue(converted_occurrence.is_generated)
        self.assertTrue(converted_occurrence.is_all_day)
        # This test is commented as cancellation controls are currently excluded
        # from the admin form. This affects some uncommented assertions below,
        # which are annotated with 'was x'
        # #######################################################################
        # # Cancel a generated occurrence
        # #######################################################################
        # form = response.follow().forms[0]
        # cancelled_occurrence = event.occurrences.all()[3]
        # self.assertFalse(cancelled_occurrence.is_protected_from_regeneration)
        # form['occurrences-3-cancel_reason'].value = 'Sold out'
        # response = form.submit('_continue')
        # event = SimpleEvent.objects.get(pk=event.pk)
        # self.assertEqual(10 + 1, event.occurrences.count())
        # cancelled_occurrence = models.Occurrence.objects.get(
        #     pk=cancelled_occurrence.pk)
        # self.assertTrue(cancelled_occurrence.is_protected_from_regeneration)
        # self.assertTrue(cancelled_occurrence.is_generated)
        # self.assertTrue(cancelled_occurrence.is_cancelled)
        # self.assertEqual('Sold out', cancelled_occurrence.cancel_reason)
        #######################################################################
        # Delete a generated occurrence (should be regenerated)
        #######################################################################
        self.assertEqual(11, event.occurrences.count())
        form = response.follow().forms[0]
        form['occurrences-8-DELETE'].value = True
        response = form.submit('_continue')
        self.assertEqual(10, event.occurrences.count())
        #######################################################################
        # Regenerate event occurrences and confirm user modifications intact
        #######################################################################
        self.assertEqual(1, event.occurrences.added_by_user().count())
        self.assertEqual(
            9,  # Down one, since we deleted a generated occurrence above
            event.occurrences.generated().count())
        self.assertEqual(3, event.occurrences.protected_from_regeneration().count()) # was 4
        self.assertEqual(7, event.occurrences.unprotected_from_regeneration().count()) # was 6
        self.assertEqual(7, event.occurrences.regeneratable().count()) # was 6
        # Regenerate!
        event.regenerate_occurrences()
        self.assertEqual(11, event.occurrences.count())
        self.assertEqual(1, event.occurrences.added_by_user().count())
        self.assertEqual(
            10,  # Deleted generated occurrence is recreated
            event.occurrences.generated().count())
        self.assertEqual(3, event.occurrences.protected_from_regeneration().count()) # was 4
        self.assertEqual(8, event.occurrences.unprotected_from_regeneration().count()) # was 7
        self.assertEqual(8, event.occurrences.regeneratable().count()) # was 7

    def test_event_publishing(self):
        #######################################################################
        # Create unpublished (draft) event
        #######################################################################
        event = G(
            SimpleEvent,
            title='Test Event',
            layout=self.layout,
        )
        self.assertTrue(event.is_draft)
        self.assertEqual([event], list(SimpleEvent.objects.draft()))
        self.assertIsNone(event.get_published())
        self.assertEqual([], list(SimpleEvent.objects.published()))
        self.assertEqual(0, event.repeat_generators.count())
        self.assertEqual(0, event.occurrences.count())
        view_response = self.app.get(
            reverse('icekit_events_eventbase_detail', args=(event.slug,)),
            expect_errors=404)
        #######################################################################
        # Publish event, nothing much to clone yet
        #######################################################################
        response = self.app.get(
            reverse('admin:icekit_events_eventbase_publish', args=(event.pk,)),
            user=self.superuser,
        )
        self.assertEqual(302, response.status_code)
        event = SimpleEvent.objects.get(pk=event.pk)
        self.assertTrue(event.is_draft)
        self.assertEqual([event], list(SimpleEvent.objects.draft()))
        self.assertIsNotNone(event.get_published())
        published_event = event.get_published()
        self.assertEqual(
            [published_event], list(SimpleEvent.objects.published()))
        self.assertEqual(event.title, published_event.title)
        self.assertEqual(0, published_event.repeat_generators.count())
        self.assertEqual(0, published_event.repeat_generators.count())

        view_response = self.app.get(
            reverse('icekit_events_eventbase_detail', args=(published_event.slug,)))
        self.assertEqual(200, view_response.status_code)
        self.assertTrue('Test Event' in view_response.content)
        #######################################################################
        # Update draft event with repeat generators and manual occurrences
        #######################################################################
        response = self.app.get(
            reverse('admin:icekit_events_eventbase_change', args=(event.pk,)),
            user=self.superuser,
        )
        form = response.forms[0]
        # Update event title
        form['title'].value += ' - Update 1'
        # Add weekly repeat for 4 weeks
        repeat_end = localize_preserving_time_of_day(
            self.start + timedelta(days=28))
        form['repeat_generators-0-recurrence_rule_0'].select(
            str(self.weekly_recurrence_rule.pk))
        form['repeat_generators-0-recurrence_rule_1'].value = 'weekly'
        form['repeat_generators-0-recurrence_rule_2'].value = "FREQ=WEEKLY"
        form['repeat_generators-0-start_0'].value = \
            self.start.strftime('%Y-%m-%d')
        form['repeat_generators-0-start_1'].value = \
            self.start.strftime('%H:%M:%S')
        form['repeat_generators-0-end_0'].value = \
            self.end.strftime('%Y-%m-%d')
        form['repeat_generators-0-end_1'].value = \
            self.end.strftime('%H:%M:%S')
        form['repeat_generators-0-repeat_end_0'].value = \
            repeat_end.strftime('%Y-%m-%d')
        form['repeat_generators-0-repeat_end_1'].value = \
            repeat_end.strftime('%H:%M:%S')
        # Add ad-hoc occurrence
        extra_occurrence_start = localize_preserving_time_of_day(
            self.start - timedelta(days=30))
        extra_occurrence_end = extra_occurrence_start + timedelta(hours=3)
        form['occurrences-0-start'].value = \
            extra_occurrence_start.strftime('%Y-%m-%d %H:%M:%S')
        form['occurrences-0-end'].value = \
            extra_occurrence_end.strftime('%Y-%m-%d %H:%M:%S')
        # Submit form
        response = form.submit(name='_continue')
        self.assertEqual(302, response.status_code)
        event = SimpleEvent.objects.get(pk=event.pk)
        # Convert a generated occurrence to all-day
        form = response.follow().forms[0]
        converted_occurrence = event.occurrences.all()[3]
        self.assertFalse(converted_occurrence.is_protected_from_regeneration)
        form['occurrences-3-is_all_day'].value = True
        response = form.submit('_continue')
        self.assertEqual(302, response.status_code)
        #######################################################################
        # Republish event, ensure everything is cloned
        #######################################################################
        # First check that published copy remains unchanged so far
        published_event = SimpleEvent.objects.get(pk=published_event.pk)
        self.assertEqual('Test Event', published_event.title)
        self.assertEqual(0, published_event.repeat_generators.count())
        self.assertEqual(0, published_event.occurrences.count())
        view_response = self.app.get(
            reverse('icekit_events_eventbase_detail', args=(published_event.slug,)))
        self.assertEqual(200, view_response.status_code)
        self.assertFalse('Test Event - Update 1' in view_response.content)
        # Republish event
        response = self.app.get(
            reverse('admin:icekit_events_eventbase_publish', args=(event.pk,)),
            user=self.superuser,
        )
        self.assertEqual(302, response.status_code)
        event = SimpleEvent.objects.get(pk=event.pk)
        # Original published event record has been deleted
        self.assertEqual(
            0, SimpleEvent.objects.filter(pk=published_event.pk).count())
        # Confirm cloning of published event's repeat rules and occurrences
        published_event = event.get_published()
        self.assertEqual('Test Event - Update 1', published_event.title)
        self.assertEqual(
            event.repeat_generators.count(),
            published_event.repeat_generators.count())
        for draft_generator, published_generator in zip(
                event.repeat_generators.all(),
                published_event.repeat_generators.all()
        ):
            self.assertNotEqual(draft_generator.pk, published_generator.pk)
            self.assertEqual(event, draft_generator.event)
            self.assertEqual(published_event, published_generator.event)
            self.assertEqual(
                draft_generator.recurrence_rule,
                published_generator.recurrence_rule)
        for draft_occurrence, published_occurrence in zip(
                event.occurrences.all(), published_event.occurrences.all()):
            self.assertNotEqual(draft_occurrence.pk, published_occurrence.pk)
            self.assertEqual(event, draft_occurrence.event)
            self.assertEqual(published_event, published_occurrence.event)
            self.assertEqual(
                draft_occurrence.start, published_occurrence.start)
            self.assertEqual(
                draft_occurrence.end, published_occurrence.end)
            self.assertEqual(
                draft_occurrence.is_all_day, published_occurrence.is_all_day)
            self.assertEqual(
                draft_occurrence.is_protected_from_regeneration,
                published_occurrence.is_protected_from_regeneration)
            self.assertEqual(
                draft_occurrence.is_cancelled,
                published_occurrence.is_cancelled)
            self.assertEqual(
                draft_occurrence.is_hidden,
                published_occurrence.is_hidden)
            self.assertEqual(
                draft_occurrence.cancel_reason,
                published_occurrence.cancel_reason)
            self.assertEqual(
                draft_occurrence.original_start,
                published_occurrence.original_start)
            self.assertEqual(
                draft_occurrence.original_end,
                published_occurrence.original_end)
        view_response = self.app.get(
            reverse('icekit_events_eventbase_detail', args=(published_event.slug,)))
        self.assertEqual(200, view_response.status_code)
        self.assertTrue('Test Event - Update 1' in view_response.content)
        #######################################################################
        # Unpublish event
        #######################################################################
        # Unpublish event
        response = self.app.get(
            reverse('admin:icekit_events_eventbase_unpublish', args=(event.pk,)),
            user=self.superuser,
        )
        self.assertEqual(302, response.status_code)
        event = SimpleEvent.objects.get(pk=event.pk)
        self.assertTrue(event.is_draft)
        self.assertIsNone(event.get_published())
        view_response = self.app.get(
            reverse('icekit_events_eventbase_detail', args=(published_event.slug,)),
            expect_errors=404)

    def test_admin_calendar(self):
        event = G(
            SimpleEvent,
            title='Test Event',
            layout=self.layout,
        )
        repeat_end = localize_preserving_time_of_day(
            self.end + timedelta(days=7))
        G(
            models.EventRepeatsGenerator,
            event=event,
            start=self.start,
            end=self.end,
            recurrence_rule="FREQ=DAILY;BYDAY=SA,SU",
            repeat_end=repeat_end,
        )
        self.assertTrue(event.occurrences.count() in [2, 3]) # 3 on weekends
        #######################################################################
        # Fetch calendar HTML page
        #######################################################################
        response = self.app.get(
            reverse('admin:icekit_events_eventbase_calendar'),
            user=self.superuser,
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual('text/html; charset=utf-8', response['content-type'])
        self.assertTrue("<div id='calendar'></div>" in response.content)
        self.assertTrue(
            reverse('admin:icekit_events_eventbase_calendar_data')
            in response.content)
        #######################################################################
        # Fetch calendar JSON data
        #######################################################################
        response = self.app.get(
            url=reverse('admin:icekit_events_eventbase_calendar_data'),
            params={
                'start': self.start.date(),
                'end': repeat_end.date() + timedelta(days=1),
            },
            user=self.superuser,
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual('application/json', response['content-type'])
        data = json.loads(response.content)
        self.assertTrue(len(data) in [2,3]) # 3 on weekends
        def format_dt_like_fullcalendar(dt):
            formatted = dt.astimezone(djtz.get_current_timezone()) \
                .strftime('%Y-%m-%dT%H:%M:%S%z')
            # FullCalendar includes ':' between hour & minute portions of the
            # timzone offset. There's no way to do this directly with Python's
            # `strftime` formatting...
            formatted = formatted[:-2] + ':' + formatted[-2:]
            return formatted
        for entry, occurrence in zip(data, event.occurrences.all()):
            self.assertEqual(
                occurrence.event.title,
                entry['title'])
            self.assertEqual(
                format_dt_like_fullcalendar(occurrence.start),
                entry['start'])
            self.assertEqual(
                format_dt_like_fullcalendar(occurrence.end),
                entry['end'])
            self.assertEqual(
                occurrence.is_all_day,
                entry['allDay'])

    # TODO Test Event cloning


class Forms(TestCase):

    def test_RecurrenceRuleField(self):
        """
        Test validation.
        """
        # Incomplete.
        message = 'Enter a complete value.'
        with self.assertRaisesMessage(ValidationError, message):
            forms.RecurrenceRuleField().clean([1, None, None])

        # Invalid.
        message = 'Enter a valid iCalendar (RFC2445) recurrence rule.'
        with self.assertRaisesMessage(ValidationError, message):
            forms.RecurrenceRuleField().clean([None, None, 'foo'])


# These take a while to run, so commenting out for the time being.
# class Migrations(TestCase):
#
#     def test_icekit_events_backwards(self):
#         """
#         Test backwards migrations.
#         """
#         call_command('migrate', 'icekit_events', 'zero')
#         call_command('migrate', 'icekit_events')
#
#     def test_icekit_events_sample_data(self):
#         """
#         Test ``sample_data`` migrations.
#         """
#         INSTALLED_APPS = settings.INSTALLED_APPS + ('icekit_events.sample_data', )
#         with override_settings(INSTALLED_APPS=INSTALLED_APPS):
#             call_command('migrate', 'icekit_events_sample_data')
#             call_command('migrate', 'icekit_events_sample_data', 'zero')


class TestRecurrenceRule(TestCase):

    def test_str(self):
        recurrence_rule = G(
            models.RecurrenceRule,
            description='description',
            recurrence_Rule='FREQ=DAILY',
        )
        self.assertEqual(six.text_type(recurrence_rule), 'description')


class TestEventModel(TestCase):

    def setUp(self):
        self.start = timeutils.round_datetime(
            when=djtz.now(),
            precision=timedelta(minutes=1),
            rounding=timeutils.ROUND_DOWN)
        self.end = self.start

    def test_modified(self):
        """
        Test that ``modified`` field is updated on save.
        """
        obj = G(SimpleEvent)
        modified = obj.modified
        obj.save()
        self.assertNotEqual(obj.modified, modified)

    def test_str(self):
        event = G(
            SimpleEvent,
            title="Event title",
        )
        occurrence = models.Occurrence(event=event)
        self.assertTrue('"Event title"' in six.text_type(occurrence))

    def test_derived_from(self):
        # Original is originating event for itself.
        event = G(SimpleEvent)
        G(
            models.EventRepeatsGenerator,
            event=event,
            start=self.start,
            end=self.start,
            recurrence_rule='FREQ=DAILY',
        )
        self.assertIsNone(event.derived_from)
        # Variation is derived from original event
        variation = event.make_variation(
            event.occurrences.all()[6])
        variation.title = 'Variation'
        variation.save()
        self.assertNotEqual(event.pk, variation.pk)
        self.assertEqual(event, variation.derived_from)

    def test_event_without_occurrences(self):
        event = G(SimpleEvent)
        self.assertEqual(0, event.occurrences.all().count())
        self.assertEqual(
            (None, None), event.get_occurrences_range())


class TestEventManager(TestCase):
    def setUp(self):
        now = djtz.now()
        self.parent_event = G(SimpleEvent)

        # occurrences in the past only
        self.child_event_1 = G(SimpleEvent, part_of=self.parent_event, title="1")
        occ1 = G(Occurrence, event=self.child_event_1, start=now-timedelta(hours=2), end=now-timedelta(hours=1))

        # occurrences in the past and the future
        self.child_event_2 = G(SimpleEvent, part_of=self.parent_event, title="2")
        occ2a = G(Occurrence, event=self.child_event_2, start=now-timedelta(hours=2), end=now-timedelta(hours=1))
        occ2b = G(Occurrence, event=self.child_event_2, start=now+timedelta(hours=1), end=now+timedelta(hours=2)) # upcoming

        # no occurrences (inherits parent occurrences)
        self.child_event_3 = G(SimpleEvent, part_of=self.parent_event, title="3")

    def test_upcoming(self):
        # fails here on travis
        self.assertEqual(
            set(SimpleEvent.objects.with_upcoming_occurrences()),
            set([self.child_event_2]))

        self.assertEqual(
            set(SimpleEvent.objects.with_no_occurrences()),
            set([self.parent_event, self.child_event_3]))

        self.assertEqual(
            set(SimpleEvent.objects.with_upcoming_or_no_occurrences()),
            set([self.parent_event, self.child_event_2, self.child_event_3]))

    def test_contained(self):
        # fails here on travis
        self.assertEqual(
            set(self.parent_event.get_children()),
            set([self.child_event_1, self.child_event_2, self.child_event_3]))
        self.assertEqual(
            set(self.parent_event.get_children().with_upcoming_occurrences()),
            set([self.child_event_2]))
        self.assertEqual(
            set(self.parent_event.get_children().with_no_occurrences()),
            set([self.child_event_3]))
        self.assertEqual(
            set(self.parent_event.get_children().with_upcoming_or_no_occurrences()),
            set([self.child_event_2, self.child_event_3]))


class TestEventRepeatOccurrencesRespectLocalTimeDefinition(TestCase):

    def setUp(self):
        self.event = G(SimpleEvent)

    def test_daily_occurrences_spanning_aus_daylight_saving_change(self):
        # Aus daylight savings +1 hour on Sun, 2 Oct 2016 at 2:00 am
        start = djtz.datetime(2016,10,1, 9,0)
        end = djtz.datetime(2016,10,1, 17)
        repeat_end = djtz.datetime(2016,10,31, 17)

        models.EventRepeatsGenerator.objects.create(
            event=self.event,
            start=start,
            end=end,
            repeat_end=repeat_end,
            recurrence_rule='RRULE:FREQ=DAILY',
        )  # This generates occurrences

        occurrences = self.event.occurrences.all()
        self.assertEquals(occurrences.count(), 31)

        st = djtz.localize(start).time()
        et = djtz.localize(end).time()
        self.assertEquals(st, time(9,0))
        self.assertEquals(et, time(17,0))

        for o in occurrences:
            self.assertEquals(djtz.localize(o.start).time(), st)
            self.assertEquals(djtz.localize(o.end).time(), et)

    def test_localize_preserving_time_of_day(self):
        # Aus daylight savings +1 hour on Sun, 1 Oct 2017 at 2:00 am
        start = djtz.datetime(2017,9,30, 9,0)
        end = djtz.datetime(2017,9,30, 11,0)

        # Direct use of `timedelta` arithmetic produces unexpected results when
        # you cross daylight savings boundaries where the time-of-day can shift
        repeat_end = start + timedelta(days=2)
        self.assertEqual(9, start.hour)
        self.assertEqual(9, repeat_end.hour)  # Seems okay so far
        # But when the value gets localized, as it will sooner or later, the
        # `timedelta` addition turns out to have triggered a time-of-day shift
        repeat_end = djtz.localize(repeat_end)
        self.assertEqual(10, repeat_end.hour)  # OOPS!
        # And to show the direct `timedelta` causes problems when used
        generator = models.EventRepeatsGenerator.objects.create(
            event=self.event,
            start=start,
            end=end,
            repeat_end=start + timedelta(days=2),
            recurrence_rule='RRULE:FREQ=DAILY',
        )  # This generates occurrences
        self.assertEquals(3, self.event.occurrences.all().count())  # WRONG!
        generator.delete()

        # Use our utility method to avoid this surprise
        repeat_end = localize_preserving_time_of_day(
            start + timedelta(days=2))
        self.assertEqual(9, repeat_end.hour)
        repeat_end = djtz.localize(repeat_end)
        self.assertEqual(9, repeat_end.hour)  # GOOD!
        # And to confirm this does the right thing when used
        models.EventRepeatsGenerator.objects.create(
            event=self.event,
            start=start,
            end=end,
            repeat_end=localize_preserving_time_of_day(
                start + timedelta(days=2)),
            recurrence_rule='RRULE:FREQ=DAILY',
        )  # This generates occurrences
        self.assertEquals(2, self.event.occurrences.all().count())  # RIGHT!


class TestEventRepeatsGeneratorModel(TestCase):

    def setUp(self):
        """ Create a daily recurring event with no end date """
        self.start = timeutils.round_datetime(
            when=djtz.now(),
            precision=timedelta(days=1),
            rounding=timeutils.ROUND_DOWN)
        self.end = self.start + appsettings.DEFAULT_ENDS_DELTA

        self.naive_start = coerce_naive(self.start)
        self.naive_end = coerce_naive(self.end)

    def test_uses_recurrencerulefield(self):
        """
        Test form field and validators.
        """
        # Form field.
        fields = fields_for_model(models.EventRepeatsGenerator)
        self.assertIsInstance(
            fields['recurrence_rule'], forms.RecurrenceRuleField)

        # Validation.
        generator = models.EventRepeatsGenerator(recurrence_rule='foo')
        message = 'Enter a valid iCalendar (RFC2445) recurrence rule.'
        with self.assertRaisesMessage(ValidationError, message):
            generator.full_clean()

    def test_save_checks(self):
        # End cannot come before start
        self.assertRaisesRegexp(
            models.GeneratorException,
            r'End date/time must be after or equal to start date/time.*',
            models.EventRepeatsGenerator.objects.create,
            start=self.start,
            end=self.start - timedelta(seconds=1),
        )
        # End can equal start
        generator = models.EventRepeatsGenerator.objects.create(
            start=self.start,
            end=self.start,
            event=G(SimpleEvent),
        )
        self.assertEqual(timedelta(), generator.duration)
        # Repeat end cannot be set without a recurrence rule
        self.assertRaisesRegexp(
            models.GeneratorException,
            'Recurrence rule must be set if a repeat end date/time is set.*',
            models.EventRepeatsGenerator.objects.create,
            start=self.start,
            end=self.start + timedelta(seconds=1),
            repeat_end=self.start + timedelta(seconds=1),
        )
        # Repeat end cannot come before start
        self.assertRaisesRegexp(
            models.GeneratorException,
            'Repeat end date/time must be after or equal to start date/time.*',
            models.EventRepeatsGenerator.objects.create,
            start=self.start,
            end=self.start + timedelta(seconds=1),
            recurrence_rule='FREQ=DAILY',
            repeat_end=self.start - timedelta(seconds=1),
        )
        # All-day generator must have a start datetime with 00:00:00 time
        self.assertRaisesRegexp(
            models.GeneratorException,
            'Start date/time must be at 00:00:00 hours/minutes/seconds for'
            ' all-day generators.*',
            models.EventRepeatsGenerator.objects.create,
            is_all_day=True,
            start=self.start.replace(hour=0, minute=0, second=1),
            end=self.start.replace(hour=0, minute=0, second=1),
        )
        # All-day generator duration must be a multiple of whole days
        models.EventRepeatsGenerator.objects.create(
            is_all_day=True,
            start=self.start,
            end=self.start + timedelta(hours=24),
            event=G(SimpleEvent),
        )

    def test_duration(self):
        self.assertEquals(
            timedelta(minutes=73),
            G(
                models.EventRepeatsGenerator,
                start=self.start,
                end=self.start + timedelta(minutes=73),
                event=G(SimpleEvent),
            ).duration
        )
        self.assertEquals(
            timedelta(),
            G(
                models.EventRepeatsGenerator,
                start=self.start,
                end=self.start,
                event=G(SimpleEvent),
            ).duration
        )
        self.assertEquals(
            timedelta(days=1, microseconds=-1),
            G(
                models.EventRepeatsGenerator,
                is_all_day=True,
                start=self.start,
                end=self.start,
                event=G(SimpleEvent),
            ).duration
        )

    def test_limited_daily_repeating_generator(self):
        generator = G(
            models.EventRepeatsGenerator,
            start=self.start,
            end=self.end,
            recurrence_rule='FREQ=DAILY',
            repeat_end=localize_preserving_time_of_day(
                self.start + timedelta(days=20)),  # Exclusive end time
        )
        # Repeating generator has expected date entries in its RRULESET
        rruleset = generator.get_rruleset()
        self.assertEqual(20, rruleset.count())
        self.assertTrue(self.naive_start in rruleset)
        self.assertTrue(self.naive_start + timedelta(days=1) in rruleset)
        self.assertTrue(self.naive_start + timedelta(days=2) in rruleset)
        self.assertTrue(self.naive_start + timedelta(days=19) in rruleset)
        self.assertFalse(self.naive_start + timedelta(days=20) in rruleset)
        # Repeating generator generates expected start/end times
        start_and_end_times_list = list(generator.generate())
        self.assertEqual(20, len(start_and_end_times_list))
        self.assertEqual(
            (self.naive_start, self.naive_end),
            start_and_end_times_list[0])
        self.assertEqual(
            (self.naive_start + timedelta(days=1), self.naive_end + timedelta(days=1)),
            start_and_end_times_list[1])
        self.assertEqual(
            (self.naive_start + timedelta(days=2), self.naive_end + timedelta(days=2)),
            start_and_end_times_list[2])
        self.assertEqual(
            (self.naive_start + timedelta(days=19), self.naive_end + timedelta(days=19)),
            start_and_end_times_list[19])
        self.assertFalse(
            (self.naive_start + timedelta(days=20), self.naive_end + timedelta(days=20))
            in start_and_end_times_list)

    def test_unlimited_daily_repeating_generator(self):
        generator = G(
            models.EventRepeatsGenerator,
            start=self.start,
            end=self.end,
            recurrence_rule='FREQ=DAILY',
        )
        # Repeating generator has expected date entries in its RRULESET
        rruleset = generator.get_rruleset()
        self.assertTrue(self.naive_start in rruleset)
        self.assertTrue(self.naive_start + timedelta(days=1) in rruleset)
        self.assertTrue(self.naive_start + timedelta(days=2) in rruleset)
        # Default ``appsettings.REPEAT_LIMIT`` is 13 weeks
        self.assertTrue(self.naive_start + timedelta(days=7 * 13) in rruleset)
        self.assertFalse(self.naive_start + timedelta(days=7 * 13 + 1) in rruleset)
        # Repeating generator generates expected start/end times
        start_and_end_times = generator.generate()
        self.assertEqual(
            (self.naive_start, self.naive_end),
            next(start_and_end_times))
        self.assertEqual(
            (self.naive_start + timedelta(days=1), self.naive_end + timedelta(days=1)),
            next(start_and_end_times))
        self.assertEqual(
            (self.naive_start + timedelta(days=2), self.naive_end + timedelta(days=2)),
            next(start_and_end_times))
        for i in range(16):
            next(start_and_end_times)
        self.assertEqual(
            (self.naive_start + timedelta(days=19), self.naive_end + timedelta(days=19)),
            next(start_and_end_times))
        # Default ``appsettings.REPEAT_LIMIT`` is 13 weeks
        for i in range(13 * 7 - 20):
            next(start_and_end_times)
        self.assertEqual(
            (self.naive_start + timedelta(days=91), self.naive_end + timedelta(days=91)),
            next(start_and_end_times))

    def test_daily_repeating_every_day_in_month(self):
        start = djtz.datetime(2016,10,1, 0,0)
        end = djtz.datetime(2016,10,1, 0,0)
        repeat_end = djtz.datetime(2016,10,31, 0,0)
        generator = G(
            models.EventRepeatsGenerator,
            start=start,
            end=end,
            is_all_day=True,
            recurrence_rule='FREQ=DAILY',
            repeat_end=repeat_end,
        )
        # Repeating generator has expected date entries in its RRULESET
        rruleset = generator.get_rruleset()
        self.assertTrue(31, rruleset.count())
        self.assertTrue(coerce_naive(start) in rruleset)
        self.assertTrue(
            coerce_naive(djtz.datetime(2016,10,31, 0,0)) in rruleset)
        # Repeating generator `generate` method produces expected date entries
        start_and_end_times_list = list(generator.generate())
        self.assertEqual(31, len(start_and_end_times_list))
        self.assertEqual(
            (coerce_naive(start),
             coerce_naive(end) + timedelta(days=1, microseconds=-1)),
            start_and_end_times_list[0])
        self.assertEqual(
            (coerce_naive(repeat_end),
             coerce_naive(repeat_end) + timedelta(days=1, microseconds=-1)),
            start_and_end_times_list[-1])


class TestEventOccurrences(TestCase):

    def setUp(self):
        """
        Create an event with a daily repeat generator.
        """
        self.start = timeutils.round_datetime(
            when=djtz.now(),
            precision=timedelta(days=1),
            rounding=timeutils.ROUND_DOWN)
        self.end = self.start + appsettings.DEFAULT_ENDS_DELTA

    def test_time_range_string(self):
        event = G(SimpleEvent)

        timed_occurrence = models.Occurrence.objects.create(
            event=event,
            start=djtz.datetime(2016,10,1, 9,0),
            end=djtz.datetime(2016,10,1, 19,0),
        )
        self.assertEquals(
            'Oct. 1, 2016 9 a.m. - Oct. 1, 2016 7 p.m.',
            timed_occurrence.time_range_string())

        single_day_all_day_occurrence = models.Occurrence.objects.create(
            event=event,
            start=djtz.datetime(2016,10,1, 0,0),
            end=djtz.datetime(2016,10,1, 0,0),
            is_all_day=True,
        )
        self.assertEquals(
            'Oct. 1, 2016, all day',
            single_day_all_day_occurrence.time_range_string())

        multi_day_all_day_occurrence = models.Occurrence.objects.create(
            event=event,
            start=djtz.datetime(2016,10,1, 0,0),
            end=djtz.datetime(2016,10,2, 0,0),
            is_all_day=True,
        )
        self.assertEquals(
            'Oct. 1, 2016 - Oct. 2, 2016, all day',
            multi_day_all_day_occurrence.time_range_string())

    def test_initial_event_occurrences_automatically_created(self):
        event = G(SimpleEvent)
        self.assertEqual(event.occurrences.count(), 0)
        # Occurrences generated for event when `EventRepeatsGenerator` added
        G(
            models.EventRepeatsGenerator,
            event=event,
            start=self.start,
            end=self.end,
            recurrence_rule='FREQ=DAILY',
            repeat_end=localize_preserving_time_of_day(
                self.start + timedelta(days=20)),  # Exclusive end time
        )
        self.assertEqual(event.occurrences.count(), 20)
        # An occurrence exists for each expected start time
        occurrence_starts, occurrence_ends = get_occurrence_times_for_event(event)
        first_occurrence = event.occurrences.all()[0]
        for days_hence in range(20):
            start = first_occurrence.start + timedelta(days=days_hence)
            self.assertTrue(
                coerce_naive(start) in occurrence_starts,
                "Missing start time %d days hence" % days_hence)
            end = first_occurrence.end + timedelta(days=days_hence)
            self.assertTrue(
                coerce_naive(end) in occurrence_ends,
                "Missing end time %d days hence" % days_hence)
        # Confirm Event correctly returns first & last occurrences
        self.assertEqual(
            self.start, event.get_occurrences_range()[0].start)
        self.assertEqual(
            self.end + timedelta(days=19),
            event.get_occurrences_range()[1].end)

    def test_limited_daily_repeating_occurrences(self):
        event = G(SimpleEvent)
        G(
            models.EventRepeatsGenerator,
            event=event,
            start=self.start,
            end=self.end,
            recurrence_rule='FREQ=DAILY',
            repeat_end=localize_preserving_time_of_day(
                self.start + timedelta(days=20)),  # Exclusive end time
        )
        self.assertEqual(20, event.occurrences.all().count())
        self.assertEqual(
            self.start,
            event.occurrences.all()[0].start)
        self.assertEqual(
            localize_preserving_time_of_day(self.start + timedelta(days=1)),
            event.occurrences.all()[1].start)
        self.assertEqual(
            localize_preserving_time_of_day(self.start + timedelta(days=2)),
            event.occurrences.all()[2].start)
        self.assertEqual(
            localize_preserving_time_of_day(self.start + timedelta(days=19)),
            event.occurrences.all()[19].start)

    def test_unlimited_daily_repeating_occurrences(self):
        event = G(SimpleEvent)
        G(
            models.EventRepeatsGenerator,
            event=event,
            start=self.start,
            end=self.end,
            recurrence_rule='FREQ=DAILY',
        )
        # Default ``appsettings.REPEAT_LIMIT`` is 13 weeks
        self.assertEqual(7 * 13 + 1, event.occurrences.all().count())
        self.assertEqual(
            self.start,
            event.occurrences.all()[0].start)
        self.assertEqual(
            localize_preserving_time_of_day(self.start + timedelta(days=1)),
            event.occurrences.all()[1].start)
        self.assertEqual(
            localize_preserving_time_of_day(self.start + timedelta(days=2)),
            event.occurrences.all()[2].start)
        self.assertEqual(
            localize_preserving_time_of_day(self.start + timedelta(days=19)),
            event.occurrences.all()[19].start)
        # Default repeat limit prevents far-future occurrences but we can
        # override that if we want
        event.extend_occurrences(
            until=self.end + timedelta(days=999, seconds=1))
        self.assertEqual(1000, event.occurrences.all().count())
        self.assertEqual(
            localize_preserving_time_of_day(self.start + timedelta(days=999)),
            event.occurrences.all()[999].start)

    def test_add_arbitrary_occurrence_to_nonrepeating_event(self):
        event = G(SimpleEvent)
        self.assertEqual(0, event.occurrences.count())
        # Add an arbitrary occurrence
        arbitrary_dt1 = localize_preserving_time_of_day(
            self.start + timedelta(days=3, hours=-2))
        added_occurrence = event.add_occurrence(arbitrary_dt1)
        # Confirm arbitrary occurrence is associated with event
        self.assertEqual(1, event.occurrences.count())
        # Confirm arbitrary occurrence has expected values
        self.assertEqual(added_occurrence, event.occurrences.all()[0])
        self.assertEqual(arbitrary_dt1, added_occurrence.start)
        self.assertEqual(
            arbitrary_dt1, added_occurrence.end)
        self.assertEqual(timedelta(), added_occurrence.duration)
        self.assertFalse(added_occurrence.is_generated)
        self.assertTrue(added_occurrence.is_protected_from_regeneration)

    def test_add_arbitrary_occurrences_to_repeating_event(self):
        arbitrary_dt1 = coerce_naive(
            self.start + timedelta(days=3, hours=-2))
        arbitrary_dt2 = coerce_naive(
            self.start + timedelta(days=7, hours=5))
        event = G(SimpleEvent)
        generator = G(
            models.EventRepeatsGenerator,
            event=event,
            start=self.start,
            end=self.end,
            recurrence_rule='FREQ=WEEKLY',
            repeat_end=localize_preserving_time_of_day(
                self.start + timedelta(days=7 * 4)),
        )
        self.assertEqual(4, event.occurrences.count())
        self.assertEqual(self.start, event.occurrences.all()[0].start)
        self.assertEqual(self.end, event.occurrences.all()[0].end)
        rruleset = generator.get_rruleset()
        self.assertEqual(4, rruleset.count())
        self.assertFalse(arbitrary_dt1 in rruleset)
        self.assertFalse(arbitrary_dt2 in rruleset)
        # Add arbitrary occurrences
        added_occurrence_1 = event.add_occurrence(arbitrary_dt1)
        added_occurrence_2 = event.add_occurrence(
            arbitrary_dt2,
            # NOTE: Custom end time, so duration will differ from Event's
            end=arbitrary_dt2 + timedelta(minutes=1))
        # Confirm arbitrary occurrences are associated with event
        self.assertEqual(6, event.occurrences.count())
        self.assertEqual(2, event.occurrences.filter(is_protected_from_regeneration=True).count())
        # Confirm arbitrary occurrences have expected values
        self.assertEqual(added_occurrence_1, event.occurrences.all()[1])
        self.assertEqual(arbitrary_dt1, added_occurrence_1.start)
        self.assertEqual(
            arbitrary_dt1, added_occurrence_1.end)
        self.assertEqual(timedelta(), added_occurrence_1.duration)
        self.assertFalse(added_occurrence_1.is_generated)
        self.assertTrue(added_occurrence_1.is_protected_from_regeneration)

        self.assertEqual(added_occurrence_2, event.occurrences.all()[3])
        self.assertEqual(arbitrary_dt2, added_occurrence_2.start)
        self.assertEqual(
            arbitrary_dt2 + timedelta(minutes=1), added_occurrence_2.end)
        self.assertEqual(timedelta(minutes=1), added_occurrence_2.duration)
        self.assertFalse(added_occurrence_2.is_generated)
        self.assertTrue(added_occurrence_2.is_protected_from_regeneration)
        # Check regenerating occurrences leaves added ones in place...
        event.regenerate_occurrences()
        self.assertTrue(added_occurrence_1 in event.occurrences.all())
        self.assertTrue(added_occurrence_2 in event.occurrences.all())

    def test_cancel_arbitrary_occurrence_from_repeating_event(self):
        event = G(SimpleEvent)
        G(
            models.EventRepeatsGenerator,
            event=event,
            start=self.start,
            end=self.end,
            recurrence_rule='FREQ=WEEKLY',
            repeat_end=localize_preserving_time_of_day(
                self.start + timedelta(days=7 * 8)),
        )
        self.assertEqual(8, event.occurrences.count())
        # Find valid occurrences to cancel
        occurrence_to_cancel_1 = event.occurrences.all()[3]
        occurrence_to_cancel_2 = event.occurrences.all()[5]

        # this also tests the cached occurrence lists
        occurrence_starts, __ = get_occurrence_times_for_event(event)
        self.assertTrue(
            coerce_naive(occurrence_to_cancel_1.start)
            in occurrence_starts)
        self.assertTrue(
            coerce_naive(occurrence_to_cancel_2.start)
            in occurrence_starts)
        # Cancel occurrences
        event.cancel_occurrence(occurrence_to_cancel_1)
        event.cancel_occurrence(
            occurrence_to_cancel_2, reason='Just because...',
            hide_cancelled_occurrence=True)
        # Check field values change as expected when an occurrence is deleted
        occurrence_to_cancel_1 = models.Occurrence.objects.get(pk=occurrence_to_cancel_1.pk)
        self.assertTrue(occurrence_to_cancel_1.is_protected_from_regeneration)
        self.assertTrue(occurrence_to_cancel_1.is_cancelled)
        self.assertFalse(occurrence_to_cancel_1.is_hidden)
        self.assertEqual('Cancelled', occurrence_to_cancel_1.cancel_reason)
        occurrence_to_cancel_2 = models.Occurrence.objects.get(pk=occurrence_to_cancel_2.pk)
        self.assertTrue(occurrence_to_cancel_2.is_protected_from_regeneration)
        self.assertTrue(occurrence_to_cancel_2.is_cancelled)
        self.assertTrue(occurrence_to_cancel_2.is_hidden)
        self.assertEqual('Just because...', occurrence_to_cancel_2.cancel_reason)
        # Confirm cancelled occurrences remain in Event's occurrences set
        self.assertEqual(8, event.occurrences.count())
        self.assertTrue(occurrence_to_cancel_1 in event.occurrences.all())
        self.assertTrue(occurrence_to_cancel_2 in event.occurrences.all())
        # Confirm we can easily filter out deleted occurrences
        self.assertEqual(6, event.occurrences.exclude(is_cancelled=True).count())
        self.assertFalse(occurrence_to_cancel_1 in event.occurrences.exclude(is_cancelled=True))
        self.assertFalse(occurrence_to_cancel_2 in event.occurrences.exclude(is_cancelled=True))
        # Check regenerating occurrences leaves cancelled ones in place...
        event.regenerate_occurrences()
        self.assertTrue(occurrence_to_cancel_1 in event.occurrences.all())
        self.assertTrue(occurrence_to_cancel_2 in event.occurrences.all())
        # ...and does not generate new occurrences in cancelled timeslots
        self.assertEqual(6, event.occurrences.exclude(is_cancelled=True).count())
        # Removing invalid occurrences has no effect
        some_other_event = G(
            SimpleEvent,
            start=self.start,
            end=self.end,
        )
        invalid_occurrence = models.Occurrence.objects.create(
            event=some_other_event,
            start=self.start + timedelta(minutes=25),
            end=self.end + timedelta(minutes=25),
        )
        event.cancel_occurrence(invalid_occurrence)
        self.assertEqual(8, event.occurrences.count())

    def test_create_missing_event_occurrences(self):
        event = G(SimpleEvent)
        G(
            models.EventRepeatsGenerator,
            event=event,
            start=self.start,
            end=self.end,
            recurrence_rule='FREQ=DAILY',
            repeat_end=localize_preserving_time_of_day(
                self.start + timedelta(days=20)),  # Exclusive end time
        )
        self.assertEqual(len(list(event.missing_occurrence_data())), 0)
        # Delete a few occurrences to simulate "missing" ones
        event.occurrences.filter(
            start__gte=event.occurrences.all()[5].start).delete()
        self.assertEqual(len(list(event.missing_occurrence_data())), 15)
        call_command('create_event_occurrences')
        self.assertEqual(len(list(event.missing_occurrence_data())), 0)
        self.assertEqual(event.occurrences.count(), 20)
        self.assertEqual(SimpleEvent.objects.count(), 1)

    def test_same_day_occurrences(self):
        event = G(SimpleEvent)
        same_day1 = G(models.Occurrence, event=event,
                 start=datetime(2016,10,01, 0,0,0),
                 end=datetime(2016,10,01, 11,59,59)
        )
        same_day2 = G(models.Occurrence, event=event,
                 start=datetime(2016,10,01, 0,0,0),
                 end=datetime(2016,10,02, 0,0,0)
        ) # midnight the next day counts as same day

        same_day3 = G(models.Occurrence, event=event,
                      start=datetime(2016, 10, 01, 14, 0, 0),
                      end=datetime(2016, 10, 01, 15, 0, 0),
                      is_all_day=True,
                      )  # midnight the next day counts as same day

        different_day1 = G(models.Occurrence, event=event,
                      start=datetime(2016, 10, 01, 0, 0, 0),
                      end=datetime(2016, 10, 31, 0, 0, 0)
        )

        different_day2 = G(models.Occurrence, event=event,
                      start=datetime(2016, 10, 01, 0, 0, 0),
                      end=datetime(2016, 10, 03, 0, 0, 0)
        )

        different_day3 = G(models.Occurrence, event=event,
                      start=datetime(2016, 10, 01, 0, 0, 0),
                      end=datetime(2016, 10, 02, 0, 0, 0),
                      is_all_day= True,
        )

        occs = event.occurrences.all()
        self.assertEquals(
            set(occs.same_day()),
            {same_day1, same_day2, same_day3}
        )
        self.assertEquals(
            set(occs.different_day()),
            {different_day1, different_day2, different_day3}
        )




class Time(TestCase):

    def test_round_datetime(self):
        m = 60
        h = m * 60
        d = h * 24
        # Input, output, precision, rounding.
        data = (
            # Round nearest.
            ((1999, 12, 31, 0, 0, 29), (1999, 12, 31, 0, 0, 0), m, timeutils.ROUND_NEAREST),
            ((1999, 12, 31, 0, 0, 30), (1999, 12, 31, 0, 1, 0), m, timeutils.ROUND_NEAREST),
            # Round up and down.
            ((1999, 12, 31, 0, 0, 29), (1999, 12, 31, 0, 1, 0), m, timeutils.ROUND_UP),
            ((1999, 12, 31, 0, 0, 30), (1999, 12, 31, 0, 0, 0), m, timeutils.ROUND_DOWN),
            # Strip microseconds.
            ((1999, 12, 31, 0, 0, 30, 999), (1999, 12, 31, 0, 1, 0), m, timeutils.ROUND_NEAREST),
            # Timedelta as precision.
            ((1999, 12, 31, 0, 0, 30), (1999, 12, 31, 0, 1, 0), timedelta(seconds=m), timeutils.ROUND_NEAREST),
            # Precisions: 5, 10, 15 20, 30 minutes, 1, 12 hours, 1 day.
            ((1999, 12, 31, 0, 2, 30), (1999, 12, 31, 0, 5, 0), m * 5, timeutils.ROUND_NEAREST),
            ((1999, 12, 31, 0, 5, 0), (1999, 12, 31, 0, 10, 0), m * 10, timeutils.ROUND_NEAREST),
            ((1999, 12, 31, 0, 7, 30), (1999, 12, 31, 0, 15, 0), m * 15, timeutils.ROUND_NEAREST),
            ((1999, 12, 31, 0, 10, 0), (1999, 12, 31, 0, 20, 0), m * 20, timeutils.ROUND_NEAREST),
            ((1999, 12, 31, 0, 15, 0), (1999, 12, 31, 0, 30, 0), m * 30, timeutils.ROUND_NEAREST),
            ((1999, 12, 31, 0, 30, 0), (1999, 12, 31, 1, 0, 0), h, timeutils.ROUND_NEAREST),
            ((1999, 12, 31, 6, 0, 0), (1999, 12, 31, 12, 0, 0), h * 12, timeutils.ROUND_NEAREST),
            ((1999, 12, 31, 12, 0, 0), (2000, 1, 1, 0, 0, 0), d, timeutils.ROUND_NEAREST),
            # Weekday as precision. 3 Jan 2000 = Monday.
            ((1999, 12, 30, 12, 0, 0), (2000, 1, 3, 0, 0, 0), timeutils.MON, timeutils.ROUND_NEAREST),
            ((1999, 12, 31, 12, 0, 0), (2000, 1, 4, 0, 0, 0), timeutils.TUE, timeutils.ROUND_NEAREST),
            ((2000, 1, 1, 12, 0, 0), (2000, 1, 5, 0, 0, 0), timeutils.WED, timeutils.ROUND_NEAREST),
            ((2000, 1, 2, 12, 0, 0), (2000, 1, 6, 0, 0, 0), timeutils.THU, timeutils.ROUND_NEAREST),
            ((2000, 1, 3, 12, 0, 0), (2000, 1, 7, 0, 0, 0), timeutils.FRI, timeutils.ROUND_NEAREST),
            ((2000, 1, 4, 12, 0, 0), (2000, 1, 8, 0, 0, 0), timeutils.SAT, timeutils.ROUND_NEAREST),
            ((2000, 1, 5, 12, 0, 0), (2000, 1, 9, 0, 0, 0), timeutils.SUN, timeutils.ROUND_NEAREST),
        )
        for dt1, dt2, precision, rounding in data:
            self.assertEqual(
                timeutils.round_datetime(datetime(*dt1), precision, rounding),
                datetime(*dt2))
