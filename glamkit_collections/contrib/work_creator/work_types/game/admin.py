import models
from django.contrib import admin
from glamkit_collections.contrib.work_creator.work_types.moving_image.admin import \
    MovingImageWorkAdmin
from icekit.content_collections.admin import TitleSlugAdmin

class GameAdmin(MovingImageWorkAdmin):
    GAME_MIXIN_FIELDSET = (
        'Game', {
            'fields': (
                'platforms',
                'is_single_player',
                'is_multi_player',
                'input_types',
            )
        }
    )
    fieldsets = MovingImageWorkAdmin.fieldsets[0:3] + (GAME_MIXIN_FIELDSET, ) + MovingImageWorkAdmin.fieldsets[3:]
    filter_horizontal = ('platforms', 'input_types',)


admin.site.register(models.GameInputType, TitleSlugAdmin)
admin.site.register(models.GamePlatform, TitleSlugAdmin)
