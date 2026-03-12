import pathlib
import runpy

runpy.run_path(str(pathlib.Path(__file__).with_name('webapp-manager.py')), run_name='__main__')
