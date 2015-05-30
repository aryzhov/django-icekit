# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from datetime import timedelta

from django.db import models, migrations
from django_dynamic_fixture import G
from timezone import timezone

from eventkit.models import Event, RecurrenceRule
from eventkit import settings
from eventkit.utils import time


def forwards(apps, schema_editor):
    """
    Create sample events for interactive testing.
    """
    starts = time.round_datetime(
        when=timezone.now(),
        precision=timedelta(days=1),
        rounding=time.ROUND_DOWN)
    ends = starts + settings.DEFAULT_ENDS_DELTA

    daily = RecurrenceRule.objects.get(description='Daily')
    weekdays = RecurrenceRule.objects.get(description='Daily, Weekdays')
    weekends = RecurrenceRule.objects.get(description='Daily, Weekends')
    weekly = RecurrenceRule.objects.get(description='Weekly')
    monthly = RecurrenceRule.objects.get(description='Monthly')
    yearly = RecurrenceRule.objects.get(description='Yearly')

    daily_event = G(
        Event,
        title='Daily Event',
        starts=starts + timedelta(hours=9),
        ends=ends + timedelta(hours=9),
        recurrence_rule=daily,
    )

    weekday_event = G(
        Event,
        title='Weekday Event',
        starts=starts + timedelta(hours=11),
        ends=ends + timedelta(hours=11),
        recurrence_rule=weekdays,
    )

    weekend_event = G(
        Event,
        title='Weekend Event',
        starts=starts + timedelta(hours=13),
        ends=ends + timedelta(hours=13),
        recurrence_rule=weekends,
    )

    weekly_event = G(
        Event,
        title='Weekly Event',
        starts=starts + timedelta(hours=15),
        ends=ends + timedelta(hours=15),
        recurrence_rule=weekly,
    )

    monthly_event = G(
        Event,
        title='Monthly Event',
        starts=starts + timedelta(hours=17),
        ends=ends + timedelta(hours=17),
        recurrence_rule=monthly,
    )

    yearly_event = G(
        Event,
        title='Yearly Event',
        starts=starts + timedelta(hours=19),
        ends=ends + timedelta(hours=19),
        recurrence_rule=yearly,
    )


def backwards(apps, schema_editor):
    titles = [
        'Daily Event',
        'Weekday Event',
        'Weekend Event',
        'Weekly Event',
        'Monthly Event',
        'Yearly Event',
    ]
    originals = Event.objects.filter(title__in=titles)
    Event.objects.filter(
        models.Q(pk__in=originals) | models.Q(original__in=originals)).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('eventkit', '0004_auto_20150514_0002'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]