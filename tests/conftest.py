# conftest.py
# ROS2 Jazzy launch_testing_ros 플러그인 충돌 근본 해결
# pluginmanager 에서 직접 언레지스터 → check_pending() 오류 방지

def pytest_configure(config):
    pm = config.pluginmanager
    for name in list(pm._name2plugin.keys()):
        if "launch" in name or "ros" in name.lower():
            try:
                plugin = pm._name2plugin[name]
                pm.unregister(plugin)
            except Exception:
                pass
