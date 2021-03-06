Contributing to the project
===========================

Please follow these guidelines when making contributions to GLAMkit.

.. toctree::
   :maxdepth: 1
   :caption: Contents:

   source-setup
   style
   testing
   documentation
   releases
   roadmap
   conduct

Installing the development version of GLAMkit on an existing project
-------------------------------------------------------------------

To use the development version of GLAMkit (rather than a pegged release) in your
docker project.

::

    $ docker-compose exec django entrypoint.sh
    $ pip install -e git+https://github.com/ic-labs/django-icekit.git@develop#egg=django-icekit

And to make this permanent, specify ``develop`` at the top of your project's
Dockerfile::

   FROM interaction/icekit:develop
   ...
