class ProductionReadOnlyRouter:
    """
    Prevents Django from running migrations or test setup against the 'production'
    database alias. All other routing is left to defaults.
    """
    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == 'production':
            return False
        return None
