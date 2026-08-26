from app.dependencies.checker import DependencyChecker, DependencySpec


def test_checker_returns_known_dependencies():
    statuses = DependencyChecker().check_all()
    names = {item.spec.name for item in statuses}
    assert {"Python", "pip", "Git", "PySide6", "pytest"}.issubset(names)


def test_missing_python_package_is_installable_spec():
    spec = DependencySpec("Example", module="module_that_should_not_exist", pip_package="example")
    status = DependencyChecker().check_one(spec)
    assert status.installed is False
