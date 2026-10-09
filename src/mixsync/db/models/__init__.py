# Importing each module registers its tables on Base.metadata.
from mixsync.db.models import auth as auth
from mixsync.db.models import infra as infra
from mixsync.db.models import safety as safety
from mixsync.db.models import work as work
