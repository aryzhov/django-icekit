"""
Models for ``icekit_events`` app.
"""

# Compose concrete models from abstract models and mixins, to facilitate reuse.
from collections import OrderedDict
from datetime import timedelta

from colorful.fields import RGBColorField
from dateutil import rrule
import six
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db.models import Q
from django.template import Context
from django.template import Template
from django.utils.functional import cached_property
from django.utils.safestring import mark_safe

from icekit_events.managers import EventManager, OccurrenceManager
from icekit_events.utils.timeutils import coerce_naive, format_naive_ical_dt, \
    zero_datetime
from timezone import timezone as djtz  # django-timezone

from django.core.urlresolvers import reverse
from django.conf import settings
from django.db import models, transaction
from django.db.models.signals import post_save, post_delete
from django.utils import encoding
from django.utils.translation import ugettext_lazy as _

from polymorphic.models import PolymorphicModel

from icekit.content_collections.abstract_models import AbstractListingPage, \
    TitleSlugMixin, PluralTitleSlugMixin
from icekit.models import ICEkitContentsMixin
from icekit.fields import ICEkitURLField
from icekit.mixins import FluentFieldsMixin
from django.template.defaultfilters import date as datefilter

from . import appsettings, validators
from .utils import timeutils


# Constant object used as a flag for unset kwarg parameters
UNSET = object()

DATE_FORMAT = settings.DATE_FORMAT
DATETIME_FORMAT = settings.DATE_FORMAT + " " + settings.TIME_FORMAT


# FIELDS ######################################################################


class RecurrenceRuleField(
        six.with_metaclass(models.SubfieldBase, models.TextField)):
    """
    A ``TextField`` subclass for iCalendar (RFC2445) recurrence rules.
    """

    default_validators = [validators.recurrence_rule]
    description = _(
        'An iCalendar (RFC2445) recurrence rule that defines when an event '
        'repeats.')

    def formfield(self, **kwargs):
        from . import forms  # Avoid circular import.
        defaults = {
            'form_class': forms.RecurrenceRuleField,
        }
        defaults.update(kwargs)
        return super(RecurrenceRuleField, self).formfield(**defaults)


# MODELS ######################################################################


class AbstractBaseModel(models.Model):
    """
    An abstract base model with common fields and methods for all models.

    Add ``created`` and ``modified`` timestamp fields. Update the ``modified``
    field automatically on save. Sort by primary key.
    """

    created = models.DateTimeField(
        default=djtz.now, db_index=True, editable=False)
    modified = models.DateTimeField(
        default=djtz.now, db_index=True, editable=False)

    class Meta:
        abstract = True
        get_latest_by = 'pk'
        ordering = ('-id', )

    def save(self, *args, **kwargs):
        """
        Update ``self.modified``.
        """
        self.modified = djtz.now()
        super(AbstractBaseModel, self).save(*args, **kwargs)


@encoding.python_2_unicode_compatible
class RecurrenceRule(AbstractBaseModel):
    """
    An iCalendar (RFC2445) recurrence rule. This model allows commonly needed
    or complex rules to be saved in advance, and then selected as needed when
    creating events.
    """
    description = models.TextField(
        help_text='Unique.',
        max_length=255,
        unique=True,
    )
    recurrence_rule = RecurrenceRuleField(
        help_text=_(
            'An iCalendar (RFC2445) recurrence rule that defines when an '
            'event repeats. Unique.'),
        unique=True,
    )

    def __str__(self):
        return self.description


class AbstractEventType(PluralTitleSlugMixin):
    is_public = models.BooleanField(
        "Show to public?",
        default=True,
        help_text="Public types are displayed to the public, e.g. 'talk', "
                  "'workshop', etc. "
                  "Non-public types are used to indicate special behaviour, "
                  "such as education or members events."
    )

    color = RGBColorField(default="#cccccc", colors=appsettings.EVENT_TYPE_COLOR_CHOICES)
    def get_absolute_url(self):
        return reverse("icekit_events_eventtype_detail", args=(self.slug, ))

    def swatch(self, color_only=False):
        return Template("""<i title="{{ o }}" style="background-color:{{ o.color }};width:1em;height:1em;display:inline-block;border-radius:50%;margin-bottom:-0.15em;"></i>{% if not color_only %}&nbsp;{{ o }}{% endif %}
        """).render(Context({'o': self, 'color_only': color_only}))

    class Meta:
        abstract = True
        # changing the verbose name rather than renaming because model rename
        # migrations are sloooooow
        verbose_name = "Event category"
        verbose_name_plural = "Event categories"


class EventType(AbstractEventType):
    pass


@encoding.python_2_unicode_compatible
class EventBase(PolymorphicModel, AbstractBaseModel, ICEkitContentsMixin,
                TitleSlugMixin):
    """
    A polymorphic event model with all basic event features.

    An event may have associated ``Occurrence``s that determine when the event
    occurs in the calendar. An event with no occurrences does not happen at
    any particular time, and will not be shown in a calendar or time-based
    view (but may be shown in other contexts).

    An event may have zero, one, or more associated ``EventRepeatsGenerator``
    instances that define the rules for automatically generating repeating
    occurrences.
    """

    objects = EventManager()

    primary_type = models.ForeignKey(
        'icekit_events.EventType',
        blank=True, null=True,
        verbose_name="Primary category",
        help_text="The primary category of this event: Talk, workshop, etc. Only "
                  "'public' event categories can be primary.",
        limit_choices_to={'is_public': True},
        related_name="events",
        on_delete=models.SET_NULL,
    )
    secondary_types = models.ManyToManyField(
        EventType, blank=True,
        verbose_name="Secondary categories",
        help_text="Additional or internal categories: Education or members events, "
                  "for example. Events show in listings for <em>every</em> category they're associated with.",
        related_name="secondary_events"
    ) # use all_types to get the union of primary and secondary types

    part_of = models.ForeignKey(
        'self',
        blank=True,
        db_index=True,
        related_name="contained_events",
        null=True,
        help_text="If this event is part of another event, select it here.",
        on_delete=models.SET_NULL
    ) # access visible contained_events via get_children()
    derived_from = models.ForeignKey(
        'self',
        blank=True,
        db_index=True,
        editable=False,
        null=True,
        related_name='derivitives',
        on_delete=models.SET_NULL
    )

    show_in_calendar = models.BooleanField(
        default=True,
        help_text=_('Show this event in the public calendar'),
    )

    human_dates = models.CharField(
        max_length=255,
        blank=True,
        help_text=_('Describe event dates in everyday language, e.g. "Every Sunday in March".'),
    )
    human_times = models.CharField(
        max_length=255,
        blank=True,
        help_text=_('Describe event times in everyday language, e.g. "10am&ndash;5pm, 8pm on Thursdays".'),
    )
    is_drop_in = models.BooleanField(
        default=False,
        help_text="Check to indicate that the event/activity can be attended at any time within the given time range."
    )
    has_tickets_available = models.BooleanField(default=False, help_text="Check to show ticketing information")
    price_line = models.CharField(
        max_length=255,
        blank=True,
        help_text='A one-line description of the price for this event, e.g. "$12 / $10 / $6"')
    price_detailed = models.TextField(
        blank=True,
        help_text=(
            'A multi-line description of the price for this event.'
            ' This is shown instead of the one-line price description if set'
        )
    )

    special_instructions = models.TextField(
        blank=True,
        help_text=_('Describe special instructions for attending event, '
                    'e.g. "Enter via the Jones St entrance".'),
    )
    external_ref = models.CharField(
        'External reference',
        max_length=255,
        blank=True, null=True,
        help_text="The reference identifier used by an external events/tickets management system."
    )
    cta_text = models.CharField(_("Call to action"),
        blank=True,
        max_length=255, default=_("Book now")
    )
    cta_url = ICEkitURLField(_("CTA URL"),
        blank=True,
        null=True,
        help_text=_('The URL where visitors can arrange to attend an event'
                    ' by purchasing tickets or RSVPing.')
    )
    location = models.ForeignKey(
        'icekit_plugins_location.Location',
        limit_choices_to={'publishing_is_draft': True},
        blank=True, null=True,
        related_name='events',
        on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ('title', 'pk')
        verbose_name = 'Event'

    def __str__(self):
        return self.title

    def __getattr__(self, attr):
        # if no sub/sister class defines this, just return own URL.
        if attr == "get_occurrence_url":
            return lambda occ: self.get_absolute_url()
        super_type = super(EventBase, self)
        if hasattr(super_type, '__getattr__'):
            return super_type.__getattr__(attr)
        else:
            return self.__getattribute__(attr)

    def get_cloneable_fieldnames(self):
        return ['title']

    def add_occurrence(self, start, end=None):
        if not end:
            end = start
        o = Occurrence.objects.create(
            event=self,
            start=start,
            end=end,
            generator=None,
            is_protected_from_regeneration=True,
        )
        self.invalidate_caches()
        return o

    def cancel_occurrence(self, occurrence, hide_cancelled_occurrence=False,
                          reason=u'Cancelled'):
        """
        "Delete" occurrence by flagging it as deleted, and optionally setting
        it to be hidden and the reason for deletion.

        We don't really delete occurrences because doing so would just cause
        them to be re-generated the next time occurrence generation is done.
        """
        if occurrence not in self.occurrence_list:
            return  # No-op
        occurrence.is_protected_from_regeneration = True
        occurrence.is_cancelled = True
        occurrence.is_hidden = hide_cancelled_occurrence
        occurrence.cancel_reason = reason
        occurrence.save()
        self.invalidate_caches()

    @transaction.atomic
    def make_variation(self, occurrence):
        """
        Make and return a variation event cloned from this event at the given
        occurrence time.
        If ``save=False`` the caller is responsible for calling ``save`` on
        both this event, and the returned variation event, to ensure they
        are saved and occurrences are refreshed.
        """
        # Clone fields from source event
        defaults = {
            field: getattr(self, field)
            for field in self.get_cloneable_fieldnames()
        }
        # Create new variation event based on source event
        variation_event = type(self)(
            derived_from=self,
            **defaults
        )
        variation_event.save()
        self.clone_event_relationships(variation_event)

        # Adjust this event so its occurrences stop at point variation splits
        self.end_repeat = occurrence.start
        qs_overlapping_occurrences = self.occurrences \
            .filter(start__gte=occurrence.start)
        qs_overlapping_occurrences.delete()

        self.save()
        return variation_event

    def missing_occurrence_data(self, until=None):
        """
        Return a generator of (start, end, generator) tuples that are the
        datetimes and generator responsible for occurrences based on any
        ``EventRepeatsGenerator``s associated with this event.

        This method performs basic detection of existing occurrences with
        matching start/end times so it can avoid recreating those occurrences,
        which will generally be user-modified items.
        """
        existing_starts, existing_ends = get_occurrence_times_for_event(self)
        for generator in self.repeat_generators.all():
            for start, end in generator.generate(until=until):
                # Skip occurrence times when we already have an existing
                # occurrence with that start time or end time, since that is
                # probably a user-modified event
                if start in existing_starts \
                        or end in existing_ends:
                    continue
                yield(start, end, generator)
        self.invalidate_caches()

    @transaction.atomic
    def extend_occurrences(self, until=None):
        """
        Create missing occurrences for this Event, assuming that existing
        occurrences are all correct (or have been pre-deleted).
        This is mostly useful for adding not-yet-created future occurrences
        with a scheduled job, e.g. via the `create_event_occurrences` command.
        Occurrences are extended up to the event's ``end_repeat`` if set, or
        the time given by the ``until`` parameter or the configured
        ``REPEAT_LIMIT`` for unlimited events.
        """
        # Create occurrences for this event
        count = 0
        for start_dt, end_dt, generator \
                in self.missing_occurrence_data(until=until):
            Occurrence.objects.create(
                event=self,
                generator=generator,
                start=start_dt,
                end=end_dt,
                original_start=start_dt,
                original_end=end_dt,
                is_all_day=generator.is_all_day,
            )
            count += 1

        self.invalidate_caches()
        return count

    @transaction.atomic
    def regenerate_occurrences(self, until=None):
        """
        Delete and re-create occurrences for this Event.
        """
        # Nuke any occurrences that are not user-modified
        self.occurrences.regeneratable().delete()
        # Generate occurrences for this event
        self.extend_occurrences(until=until)

    def publishing_clone_relations(self, src_obj):
        super(EventBase, self).publishing_clone_relations(src_obj)
        src_obj.clone_event_relationships(self)

    def clone_event_relationships(self, dst_obj):
        """
        Clone related `EventRepeatsGenerator` and `Occurrence` relationships
        from a source to destination event.
        """
        # Clone only the Occurrences that weren't
        # (auto-generated and not modified) - all others will be
        # auto-generated by the generators cloned above.
        # NOTE: Occurrences *must* be cloned first to ensure later occurrence
        # generation by cloned generators are aware of user-modifications.

        # include occurrences that weren't generated OR were user-modified.
        for occurrence in self.occurrences.filter(Q(generator__isnull=True) | Q(is_protected_from_regeneration=True)):
            occurrence.pk = None
            occurrence.event = dst_obj
            occurrence.save()
        for generator in self.repeat_generators.all():
            generator.pk = None
            generator.event = dst_obj
            generator.save()

    @cached_property
    def visible_part_of(self):
        if self.part_of_id:
            return self.part_of.get_visible()
        return None

    @cached_property
    def own_occurrences(self):
        """
        The value is returned as a list, to emphasise its cached/resolved nature.
        Manipulating a queryset would lose the benefit of caching.

        :return: A list of occurrences directly attached to the event. Used to 
        fall back to `part_of` occurrences.         
        """
        return list(self.occurrences.all())

    @cached_property
    def occurrence_list(self):
        """
        :return: A list of my occurrences, or those of my visible_part_of event
        """
        o = self.own_occurrences
        if o:
            return o
        if self.visible_part_of:
            return self.visible_part_of.occurrence_list
        return []

    @cached_property
    def upcoming_occurrence_list(self):
        if self.own_occurrences:
            return list(self.occurrences.upcoming())

        if self.visible_part_of:
            return self.visible_part_of.upcoming_occurrence_list
        return []

    def invalidate_caches(self):
        """
        Call this to clear out cached values, in case you want to access cached
         properties after changing occurrences.
        """
        try:
            del self.visible_part_of
        except AttributeError:
            pass

        try:
            del self.occurrence_list
        except AttributeError:
            pass

        try:
            del self.own_occurrences
        except AttributeError:
            pass

        try:
            del self.upcoming_occurrence_list
        except AttributeError:
            pass

    def get_occurrences_range(self):
        """
        Return the first and last chronological `Occurrence` for this event.
        """

        # TODO: if the event has a generator that never ends, return "None"
        # for the last item.
        try:
            first = self.occurrence_list[0]
        except IndexError:
            first = None

        try:
            last = self.occurrence_list[-1]
        except IndexError:
            last = None
        return (first, last)

    def get_upcoming_occurrences_by_day(self):
        """
        :return: an iterable of (day, occurrences)
        """
        result = OrderedDict()

        for occ in self.upcoming_occurrence_list:
            result.setdefault(occ.local_start.date(), []).append(occ)

        return result.items()


    def start_dates_set(self):
        """
        :return: a sorted set of all the different dates that this event
        happens on.
        """
        occurrences = [o for o in self.occurrence_list if not o.is_cancelled]
        dates = set([o.local_start.date() for o in occurrences])
        sorted_dates = sorted(dates)
        return sorted_dates

    def start_times_set(self):
        """
        :return: a sorted set of all the different times that this event
        happens on.
        """
        occurrences = [o for o in self.occurrence_list if not o.is_cancelled and not o.is_all_day]
        times = set([o.local_start.time() for o in occurrences])
        sorted_times = sorted(times)
        return sorted_times

    def get_absolute_url(self):
        return reverse('icekit_events_eventbase_detail', args=(self.slug,))

    def get_children(self):
        return EventBase.objects.filter(part_of_id=self.get_draft().id)

    def get_cta(self):
        if self.cta_url and self.cta_text:
            return self.cta_url, self.cta_text
        return ()

    def get_type(self):
        if self.primary_type:
            return self.primary_type
        return type(self)._meta.verbose_name

    def get_all_types(self):
        return self.secondary_types.all() | EventType.objects.filter(id__in=[self.primary_type_id])

    def is_educational(self):
        return self.get_all_types().filter(slug='education')

    def is_members(self):
        return self.get_all_types().filter(slug='members')

    def is_upcoming(self):
        return len(self.upcoming_occurrence_list) != 0

    def get_next_occurrence(self):
        try:
            return self.upcoming_occurrence_list[0]
        except IndexError:
            return None

    def has_finished(self):
        """
        :return: True if:
            There are occurrences, and
            There are no upcoming occurrences
        """

        return self.occurrence_list and not self.upcoming_occurrence_list

    # Commenting for now, because it sits earlier in the MRO than a
    # project-specific mixin.
    # def get_occurrence_url(self, occurrence):
    #     # Calculate and return the URL for an occurrence
    #     try:
    #         URLValidator()(occurrence.external_ref)
    #         return occurrence.external_ref
    #     except ValidationError:
    #         return ""


class AbstractEventWithLayouts(EventBase, FluentFieldsMixin):

    class Meta:
        abstract = True

    @property
    def template(self):
        return self.get_layout_template_name()


class GeneratorException(Exception):
    pass


@encoding.python_2_unicode_compatible
class AbstractEventRepeatsGenerator(AbstractBaseModel):
    """
    A model storing the information and features required to generate a set
    of repeating datetimes for a given repeat rule.

    If the event is an all day event (`all_day` has been marked as
    `True`) then the `date_starts` field will be required.

    If the event is not an all day event then the `start` field will
    be required which stores the time and date of when the event
    occurs.

    There are checks for this within the models `clean` method but this
    will not get called if the `save` method is called explicitly
    therefore care should be taken when using the `save` method to
    ensure the data meets these standards. Calling the `clean` method
    explicitly is most likely the easiest way to ensure this.
    """
    event = models.ForeignKey(
        'icekit_events.EventBase',
        db_index=True,
        editable=False,
        related_name='repeat_generators',
        on_delete=models.CASCADE
    )
    recurrence_rule = RecurrenceRuleField(
        blank=True,
        help_text=_(
            'An iCalendar (RFC2445) recurrence rule that defines when this '
            'event repeats.'),
        null=True,
    )
    start = models.DateTimeField(
        'first start',
        db_index=True)
    end = models.DateTimeField(
        'first end',
        db_index=True)
    is_all_day = models.BooleanField(
        default=False, db_index=True)
    repeat_end = models.DateTimeField(
        blank=True,
        help_text=_('If empty, this event will repeat indefinitely.'),
        null=True,
    )

    class Meta:
        abstract = True
        ordering = ['pk']  # Order by PK, essentially in creation order

    def __str__(self):
        return u"EventRepeatsGenerator of '{0}'".format(self.event.title)

    def generate(self, until=None):
        """
        Return a list of datetime objects for event occurrence start and end
        times, up to the given ``until`` parameter, or up to the ``repeat_end``
        time, or to the configured ``REPEAT_LIMIT`` for unlimited events.
        """
        # Get starting datetime just before this event's start date or time
        # (must be just before since start & end times are *excluded* by the
        # `between` method below)
        start_dt = self.start - timedelta(seconds=1)
        # Limit `until` to provided or configured maximum, unless event has its
        # own `repeat_end` which is always respected.
        if until is None:
            if self.repeat_end:
                until = self.repeat_end
            else:
                until = djtz.now() + appsettings.REPEAT_LIMIT
            # For all-day occurrence generation, make the `until` constraint
            # the next date from of the repeat end date to ensure the end
            # date is included in the generated set as users expect (and
            # remembering the `between` method used below is exclusive).
            if self.is_all_day:
                until += timedelta(days=1)
        # Make datetimes naive, since RRULE spec contains naive datetimes so
        # our constraints must be the same
        start_dt = coerce_naive(start_dt)
        until = coerce_naive(until)
        # Determine duration to add to each start time
        occurrence_duration = self.duration or timedelta(days=1)
        # `start_dt` and `until` datetimes are exclusive for our rruleset
        # lookup and will not be included
        rruleset = self.get_rruleset(until=until)
        return (
            (start, start + occurrence_duration)
            for start in rruleset.between(start_dt, until)
        )

    def get_rruleset(self, until=None):
        """
        Return an ``rruleset`` object representing the start datetimes for this
        generator, whether for one-time events or repeating ones.
        """
        # Parse complete RRULE spec into iterable rruleset
        return rrule.rrulestr(
            self._build_complete_rrule(until=until),
            forceset=True)

    def _build_complete_rrule(self, start_dt=None, until=None):
        """
        Convert recurrence rule, start datetime and (optional) end datetime
        into a full iCAL RRULE spec.
        """
        if start_dt is None:
            start_dt = self.start
        if until is None:
            until = self.repeat_end \
                or djtz.now() + appsettings.REPEAT_LIMIT
        # We assume `recurrence_rule` is always a RRULE repeat spec of the form
        # "FREQ=DAILY", "FREQ=WEEKLY", etc?
        rrule_spec = "DTSTART:%s" % format_naive_ical_dt(start_dt)
        if not self.recurrence_rule:
            rrule_spec += "\nRDATE:%s" % format_naive_ical_dt(start_dt)
        else:
            rrule_spec += "\nRRULE:%s" % self.recurrence_rule
            # Apply this event's end repeat date as an *exclusive* UNTIL
            # constraint. UNTIL in RRULE specs is inclusive by default, so we
            # fake exclusivity by adjusting the end time by a microsecond.
            if self.is_all_day:
                # For all-day generator, make the UNTIL constraint the last
                # microsecond of the repeat end date to ensure the end date is
                # included in the generated set as users expect.
                until += timedelta(days=1, microseconds=-1)
            else:
                until -= timedelta(microseconds=1)
            rrule_spec += ";UNTIL=%s" % format_naive_ical_dt(until)
        return rrule_spec

    def save(self, *args, **kwargs):
        # End time must be equal to or after start time
        if self.end < self.start:
            raise GeneratorException(
                'End date/time must be after or equal to start date/time:'
                ' {0} < {1}'.format(self.end, self.start)
            )

        if self.repeat_end:
            # A repeat end date/time requires a recurrence rule be set
            if not self.recurrence_rule:
                raise GeneratorException(
                    'Recurrence rule must be set if a repeat end date/time is'
                    ' set: {0}'.format(self.repeat_end)
                )
            # Repeat end time must be equal to or after start time
            if self.repeat_end < self.start:
                raise GeneratorException(
                    'Repeat end date/time must be after or equal to start'
                    ' date/time: {0} < {1}'.format(self.repeat_end, self.start)
                )

        if self.is_all_day:
            # An all-day generator's start time must be at 00:00
            naive_start = coerce_naive(self.start)
            if naive_start.hour or naive_start.minute or naive_start.second \
                    or naive_start.microsecond:
                raise GeneratorException(
                    'Start date/time must be at 00:00:00 hours/minutes/seconds'
                    ' for all-day generators: {0}'.format(naive_start)
                )

        # Convert datetime field values to date-compatible versions in the
        # UTC timezone when we save an all-day occurrence
        if self.is_all_day:
            self.start = zero_datetime(self.start)
            self.end = zero_datetime(self.end) \
                + timedelta(days=1, microseconds=-1)

        super(AbstractEventRepeatsGenerator, self).save(*args, **kwargs)

    @property
    def duration(self):
        """
        Return the duration between ``start`` and ``end`` as a timedelta.
        """
        return self.end - self.start


class EventRepeatsGenerator(AbstractEventRepeatsGenerator):
    pass


@encoding.python_2_unicode_compatible
class AbstractOccurrence(AbstractBaseModel):
    """
    A specific occurrence of an Event with start and end date times, and
    a reference back to the owner event that contains all the other data.
    """
    objects = OccurrenceManager()

    event = models.ForeignKey(
        'icekit_events.EventBase',
        db_index=True,
        editable=False,
        related_name='occurrences',
        on_delete=models.CASCADE
    )
    generator = models.ForeignKey(
        'icekit_events.EventRepeatsGenerator',
        blank=True, null=True,
        on_delete=models.SET_NULL
    )
    start = models.DateTimeField(
        db_index=True)
    end = models.DateTimeField(
        db_index=True)
    is_all_day = models.BooleanField(
        default=False, db_index=True)

    is_protected_from_regeneration = models.BooleanField(
        "is protected",
        default=False, db_index=True,
        help_text="if this is true, the occurrence won't be deleted when occurrences are regenerated"
    )

    is_cancelled = models.BooleanField(
        default=False)
    is_hidden = models.BooleanField(
        default=False)
    cancel_reason = models.CharField(
        max_length=255,
        blank=True, null=True)

    external_ref = models.CharField(
        max_length=255,
        blank=True, null=True,
    )

    status = models.CharField(
        max_length=255,
        blank=True, null=True,
    )

    # Start/end times as originally set by a generator, before user modifiction
    original_start = models.DateTimeField(
        blank=True, null=True, editable=False)
    original_end = models.DateTimeField(
        blank=True, null=True, editable=False)

    class Meta:
        abstract = True
        ordering = ['start', '-is_all_day', 'event', 'pk']

    def time_range_string(self):
        if self.is_all_day:
            if self.duration < timedelta(days=1):
                return u"""{0}, all day""".format(
                    datefilter(self.local_start, DATE_FORMAT))
            else:
                return u"""{0} - {1}, all day""".format(
                    datefilter(self.local_start, DATE_FORMAT),
                    datefilter(self.local_end, DATE_FORMAT))
        else:
            return u"""{0} - {1}""".format(
                datefilter(self.local_start, DATETIME_FORMAT),
                datefilter(self.local_end, DATETIME_FORMAT))

    def __str__(self):
        return u"""Occurrence of "{0}" {1}""".format(
            self.event.title,
            self.time_range_string()
        )

    @property
    def local_start(self):
        return djtz.localize(self.start)

    @property
    def local_end(self):
        return djtz.localize(self.end)

    @property
    def is_generated(self):
        return self.generator is not None

    @property
    def duration(self):
        """
        Return the duration between ``start`` and ``end`` as a timedelta.
        """
        return self.end - self.start

    @transaction.atomic
    def save(self, *args, **kwargs):
        if getattr(self, '_flag_user_modification', False):
            self.is_protected_from_regeneration = True
            # If and only if a Cancel reason is given, flag the Occurrence as
            # cancelled
            if self.cancel_reason:
                self.is_cancelled = True
            else:
                self.is_cancelled = False
        # Convert datetime field values to date-compatible versions in the
        # UTC timezone when we save an all-day occurrence
        if self.is_all_day:
            self.start = zero_datetime(self.start)
            self.end = zero_datetime(self.end)
        # Set original start/end times, if necessary
        if not self.original_start:
            self.original_start = self.start
        if not self.original_end:
            self.original_end = self.end
        super(AbstractOccurrence, self).save(*args, **kwargs)

    # TODO Return __str__ as title for now, improve it later
    def title(self):
        return unicode(self)

    def is_past(self):
        """
        :return: True if this occurrence is entirely in the past
        """
        return self.end < djtz.now()

    def get_absolute_url(self):
        return self.event.get_occurrence_url(self)


class Occurrence(AbstractOccurrence):
    pass


def get_occurrence_times_for_event(event):
    """
    Return a tuple with two sets containing the (start, end) *naive* datetimes
    of an Event's Occurrences, or the original start datetime if an
    Occurrence's start was modified by a user.
    """
    occurrences_starts = set()
    occurrences_ends = set()
    for o in event.occurrence_list:
        occurrences_starts.add(
            coerce_naive(o.original_start or o.start)
        )
        occurrences_ends.add(
            coerce_naive(o.original_end or o.end)
        )
    return occurrences_starts, occurrences_ends


class AbstractEventListingPage(AbstractListingPage):

    class Meta:
        abstract = True
        verbose_name = "Event Listing"

    def get_public_items(self, request):
        return Occurrence.objects.published()\
            .filter(event__show_in_calendar=True, is_hidden=False)

    def get_visible_items(self, request):
        return Occurrence.objects.visible()


class AbstractEventListingForDatePage(AbstractListingPage):

    class Meta:
        abstract = True

    def get_start(self, request):
        try:
            start = djtz.parse('%s 00:00' % request.GET.get('date'))
        except ValueError:
            start = djtz.midnight()
        return start

    def get_days(self, request):
        try:
            days = int(request.GET.get('days', appsettings.DEFAULT_DAYS_TO_SHOW))
        except ValueError:
            days = appsettings.DEFAULT_DAYS_TO_SHOW
        return days

    def _occurrences_on_date(self, request):
        days = self.get_days(request)
        start = self.get_start(request)
        end = start + timedelta(days=days)
        return Occurrence.objects.overlapping(start, end)

    def get_items_to_list(self, request):
        return self._occurrences_on_date(request).published()\
            .filter(event__show_in_calendar=True, is_hidden=False)

    def get_items_to_mount(self, request):
        return self._occurrences_on_date(request).visible()


def regenerate_event_occurrences(sender, instance, **kwargs):
    try:
        e = instance.event
    except EventBase.DoesNotExist:
        # this can happen if deleting an EventRepeatsGenerator as part of
        # deleting an event
        return
    e.regenerate_occurrences()
post_save.connect(regenerate_event_occurrences, sender=EventRepeatsGenerator)
post_delete.connect(regenerate_event_occurrences, sender=EventRepeatsGenerator)
