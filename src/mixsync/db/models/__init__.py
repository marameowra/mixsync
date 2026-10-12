# Importing each module registers its tables on Base.metadata.
from mixsync.db.models import accounts as accounts
from mixsync.db.models import auth as auth
from mixsync.db.models import discovery as discovery
from mixsync.db.models import infra as infra
from mixsync.db.models import library as library
from mixsync.db.models import matching as matching
from mixsync.db.models import safety as safety
from mixsync.db.models import watchlist as watchlist
from mixsync.db.models import work as work
