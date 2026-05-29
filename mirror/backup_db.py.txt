import shutil
from datetime import datetime

backup_name = f"signals_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
shutil.copy2("signals.db", backup_name)
print(f"✓ Backup created: {backup_name}")