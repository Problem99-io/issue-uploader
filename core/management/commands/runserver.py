from django.contrib.staticfiles.management.commands.runserver import Command as StaticFilesRunserverCommand


class Command(StaticFilesRunserverCommand):
    default_port = '4200'
