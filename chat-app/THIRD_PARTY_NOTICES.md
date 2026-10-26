# Third-party notices

Chat-agent source uses Apache-2.0. Dependencies remain under their own licenses.
The browser bundle includes Vue (including @vue runtime packages, Evan You and
Vue contributors, MIT) and Marked (Christopher Jeffrey and contributors, MIT).
Their complete notices are preserved in licenses/vue.txt and licenses/marked.txt
and copied to dist/licenses/dependencies during every frontend build.

Backend dependencies include FastAPI (MIT), Starlette (BSD-3-Clause), Pydantic
(MIT), HTTPX (BSD-3-Clause), Uvicorn (BSD-3-Clause), and their transitive packages.
Python package distributions carry their own notices. Development dependencies
include Vite (MIT) and @vitejs/plugin-vue (MIT). Installed dependency versions
and license metadata should be checked when preparing a release.
