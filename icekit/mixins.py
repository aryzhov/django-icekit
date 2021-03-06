import re
import json
import urllib
from urlparse import urljoin

from django.conf import settings
from icekit.admin_tools.utils import admin_url
from icekit.utils.attributes import first_of
from unidecode import unidecode

from django.db import models
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.template.defaultfilters import striptags
from django.utils.translation import ugettext_lazy as _

from fluent_contents.models import \
    ContentItemRelation, Placeholder, PlaceholderRelation
from fluent_contents.rendering import render_content_items

from icekit.tasks import store_readability_score
from icekit.utils.readability.readability import Readability
from icekit import managers


class LayoutFieldMixin(models.Model):
    """
    Add ``layout`` field to models that already have ``contentitem_set`` and
    ``placeholder_set`` fields.
    """
    layout = models.ForeignKey(
        'icekit.Layout',
        blank=True,
        null=True,
        related_name='%(app_label)s_%(class)s_related',
    )

    fallback_template = 'icekit/layouts/fallback_default.html'

    class Meta:
        abstract = True

    def get_layout_template_name(self):
        """
        Return ``layout.template_name`` or `fallback_template``.
        """
        if self.layout:
            return self.layout.template_name
        return self.fallback_template

    # HACK: This is needed to work-around a `django-fluent-contents` issue
    # where it cannot handle placeholders being added to a template after an
    # object already has placeholder data in the database.
    # See: https://github.com/edoburu/django-fluent-contents/pull/63
    def add_missing_placeholders(self):
        """
        Add missing placeholders from templates. Return `True` if any missing
        placeholders were created.
        """
        content_type = ContentType.objects.get_for_model(self)
        result = False
        if self.layout:
            for data in self.layout.get_placeholder_data():
                placeholder, created = Placeholder.objects.update_or_create(
                    parent_type=content_type,
                    parent_id=self.pk,
                    slot=data.slot,
                    defaults=dict(
                        role=data.role,
                        title=data.title,
                    ))
                result = result or created
        return result


class FluentFieldsMixin(LayoutFieldMixin):
    """
    Add ``layout``, ``contentitem_set`` and ``placeholder_set`` fields so we
    can add modular content with ``django-fluent-contents``.
    """
    contentitem_set = ContentItemRelation()
    placeholder_set = PlaceholderRelation()

    class Meta:
        abstract = True

    def placeholders(self):
        # return a dict of placeholders, organised by slot, for access in
        # templates use `page.placeholders.<slot_name>.get_content_items` to
        # test if a placeholder has any items.
        return dict([(p.slot, p)
                     for p in self.placeholder_set.all().select_related()])


# TODO: should be a sub-app.
class ReadabilityMixin(models.Model):
    readability_score = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        null=True
    )

    class Meta:
        abstract = True

    def extract_text(self):
        # return the rendered content, with HTML tags stripped.
        html = render_content_items(
            request=None, items=self.contentitem_set.all())
        return striptags(html)

    def calculate_readability_score(self):
        try:
            return Readability(unidecode(self.extract_text())).SMOGIndex()
        except:
            return None

    def store_readability_score(self):
        store_readability_score.delay(
            self._meta.app_label, self._meta.model_name, self.pk)

    def save(self, *args, **kwargs):
        r = super(ReadabilityMixin, self).save(*args, **kwargs)
        self.store_readability_score()
        return r


class ListableMixin(models.Model):
    """
    Mixin for showing content in lists. Lists normally have:
    * Type
    * Title
    * Image
    * URL (assume get_absolute_url)
    Optional oneliner (implement `get_oneliner()`)

    ...and since they show in lists, they show in search results, so
    this model also includes search-related fields.
    """
    list_image = models.ImageField(
        blank=True,
        upload_to="icekit/listable/list_image/",
        help_text="image to use in listings. Default image is used if this isn't given"
    )
    boosted_search_terms = models.TextField(
        blank=True,
        help_text=_(
            'Words (space-separated) added here are boosted in relevance for search results '
            'increasing the chance of this appearing higher in the search results.'
        ),
    )

    class Meta:
        abstract = True

    def __getattr__(self, item):
        """Only called if no class in the MRO defines the function"""
        if item == 'get_list_image':
            return self.__get_list_image
        super_type = super(ListableMixin, self)
        if hasattr(super_type, '__getattr__'):
            return super_type.__getattr__(item)
        else:
            return self.__getattribute__(item)

    def __get_list_image(self):
        """
        :return: the ImageField to use for thumbnails in lists
        NB note that the Image Field is returned, not the ICEkit Image model as
        with get_hero_image (since the override is just a field and we don't
        need alt text), not Image record.
        """
        list_image = first_of(
            self,
            'list_image',
            'get_hero_image',
            'image',
        )

        # return the `image` attribute (being the ImageField of the Image
        # model) if there is one.
        return getattr(list_image, "image", list_image)

    def get_type(self):
        """
        :return: a string OR object (with a get_absolute_url) indicating the public type of this object
        """
        return type(self)._meta.verbose_name

    def get_type_plural(self):
        """
        :return: a string (event if get_type is an object) indicating the plural of the type name
        """
        t = self.get_type()
        if t:
            if hasattr(t, 'get_plural'):
                return t.get_plural()
            if t == type(self)._meta.verbose_name:
                return unicode(type(self)._meta.verbose_name_plural)
            return u"{0}s".format(t)

        return unicode(type(self)._meta.verbose_name_plural)

    def get_title(self):
        return self.title

    def get_boosted_search_terms(self):
        return self.boosted_search_terms

    def get_oneliner(self):
        return getattr(self, 'oneliner', "")

    def get_og_title(self):
        """
        return meta_title if exists otherwise fall back to title
        """
        if hasattr(self, 'meta_title') and self.meta_title:
            return self.meta_title
        return self.get_title()

    def get_og_image_url(self):
        """
        :return: URL of the image to use in OG shares
        """
        li = self.get_list_image()
        if li:
            from easy_thumbnails.files import get_thumbnailer
            thumb_url = get_thumbnailer(li)['og_image'].url
            # TODO: looks like this may fail if SITE_DOMAIN = "acmi.lvh.me"
            return urljoin(settings.SITE_DOMAIN, thumb_url)

    def get_og_description(self):
        if hasattr(self, 'meta_description') and self.meta_description:
            return self.meta_description
        return self.get_oneliner()

    def get_admin_url(self):
        return admin_url(self)

    def get_admin_link(self):
        return u"<a href='{0}'>{1}</a>".format(self.get_admin_url(), self.get_title())


class HeroMixin(models.Model):
    """
    Mixin for adding hero content
    """
    hero_image = models.ForeignKey(
        'icekit_plugins_image.Image',
        help_text='The hero image for this content.',
        related_name="+",
        blank=True, null=True,
        on_delete=models.SET_NULL,
    )

    class Meta:
        abstract = True

    def get_hero_image(self):
        """ Return the Image record to use as the Hero """
        return self.hero_image


class GoogleMapMixin(models.Model):
    """
    Mixin for models that will store location data and be able to render a
    Google Map for the location.
    """
    objects = managers.GoogleMapManager()

    GOOGLE_MAPS_HREF_ROOT = '//maps.google.com/maps?'
    DEFAULT_MAP_ZOOM = 15

    map_description = models.TextField(
        help_text=_('A textual description of the map.'),
    )

    map_zoom = models.PositiveIntegerField(
        default=DEFAULT_MAP_ZOOM,
        help_text='''
            A positive number that indicates the zoom level of the map and
            defaults to {}.
            <br>
            Maps on Google Maps have an integer 'zoom level' which defines the
            resolution of the current view. Zoom levels between 0 (the lowest
            zoom level, in which the entire world can be seen on one map) and
            21+ (down to streets and individual buildings) are possible within
            the default roadmap view.
        '''.format(DEFAULT_MAP_ZOOM)
    )

    map_center_lat = models.DecimalField(
        blank=True,
        null=True,
        max_digits=9,
        decimal_places=6,
        help_text='''
            Latitude of map's center point.
            <br/>
            If set you must also set the map center longitude, and must not set
            the map center description
        '''
    )
    map_center_long = models.DecimalField(
        blank=True,
        null=True,
        max_digits=9,
        decimal_places=6,
        help_text='''
            Longitude of map's center point.
            <br/>
            If set you must also set the map center latitude, and must not set
            the map center description
        '''
    )
    map_center_description = models.CharField(
        max_length=255,
        blank=True,
        help_text='''
            Map's description or address.
            <br />
            If set you must not set the map center latitude or longitude.
            <br />
            Examples:
            <br><br><em>San Francisco Museum of Modern Art</em>
            <br><br><em>or</em>
            <br><br><em>151 3rd St, San Francisco, CA 94103</em>
        '''
    )

    map_marker_lat = models.DecimalField(
        blank=True,
        null=True,
        max_digits=9,
        decimal_places=6,
        help_text='''
            Latitude of map's marker point.
            <br/>
            If set you must also set the map marker longitude, and must not set
            the map marker description
        '''
    )
    map_marker_long = models.DecimalField(
        blank=True,
        null=True,
        max_digits=9,
        decimal_places=6,
        help_text='''
            Longitude of map's marker point.
            <br/>
            If set you must also set the map marker latitude, and must not set
            the map marker description
        '''
    )
    map_marker_description = models.CharField(
        max_length=255,
        blank=True,
        help_text='''
            An override for the map's marker, which defaults to the center of
            the map.
            <br>
            The value should take a description or address.
        '''
    )

    class Meta:
        abstract = True

    def clean(self):
        super(GoogleMapMixin, self).clean()
        # If the `map_center_description` field isn't shown in the
        # CMS admin, automatically clear this field value if it exists
        if (
            self.map_center_description and
            not getattr(settings, 'GOOGLE_MAP_PERMISSIVE_CENTER', True)
        ):
            self.map_center_description = ''
        # Validate center location
        if (
            not self.map_center_description and
            not self.map_center_lat and
            not self.map_center_long
        ):
            raise ValidationError(
                'Either latitude/longitude for map center or the map center'
                ' description must be defined'
            )
        if self.map_center_description:
            if self.map_center_lat or self.map_center_long:
                raise ValidationError(
                    'Latitude and longitude for the map center must not be'
                    ' defined if the map center description is defined'
                )
        else:  # no map_center_description
            if not self.map_center_lat or not self.map_center_long:
                raise ValidationError(
                    'Latitude and longitude must both be defined for map'
                    ' center if one is defined'
                )
        # Validate marker location
        if self.map_marker_description:
            if self.map_marker_lat or self.map_marker_long:
                raise ValidationError(
                    'Latitude and longitude must not be defined for marker if'
                    ' the map marker description is defined'
                )
        else:  # no map_marker_description
            if (
                (self.map_marker_lat and not self.map_marker_long) or
                (self.map_marker_long and not self.map_marker_lat)
            ):
                raise ValidationError(
                    'Latitude and longitude must both be defined for marker if'
                    ' one is defined'
                )

    def render_map(self):
        """
        Renders a container and JSON that is picked up by
        `static/icekit/js/google_map.js` which mounts a responsive static map
        with overlays and links
        """
        return (
            '<div id="{container_id}" class="google-map"></div>'
            '<script>'
            '    gkGoogleMaps = window.gkGoogleMaps || [];'
            '    gkGoogleMaps.push({data});'
            '</script>'
        ).format(
            container_id=self.get_map_element_id(),
            data=json.dumps(self.get_map_data()),
        )

    def get_map_data(self):
        """
        Returns a serializable data set describing the map location
        """

        return {
            'containerSelector': '#' + self.get_map_element_id(),
            'center': self.map_center_description,
            'marker': self.map_marker_description or self.map_center_description,
            'zoom': self.map_zoom,
            'href': self.get_map_href(),
            'key': getattr(settings, 'GOOGLE_MAPS_API_KEY', ''),
            # Python's line-splitting is more cross-OS compatible, so we feed
            # a pre-built array to the front-end
            'description': [
                line for line in self.map_description.splitlines() if line
            ],
        }

    def get_map_href(self):
        """
        Returns a link to an external view of the map
        """
        if self.map_center_lat and self.map_center_long:
            params = {
                'll': '%s,%s' % (self.map_center_lat, self.map_center_long)
            }
        else:
            params = {'q': self.map_center_description}

        return self.GOOGLE_MAPS_HREF_ROOT + urllib.urlencode(params)

    def get_map_element_id(self):
        """
        Returns a unique identifier for a map element
        """
        return 'google-map-' + str(id(self))
