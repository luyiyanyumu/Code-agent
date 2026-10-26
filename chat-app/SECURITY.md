# Security

The default server binds only to localhost. External binding requires an explicit
CHAT_AGENT_ACCESS_TOKEN. All REST conversation routes and WebSocket connections
use that token. This version supports one local owner per service instance;
it is not a multi-tenant identity service. Deploy separate instances for separate
owners. Use HTTPS when accessing a remote instance.

Model API keys are stored in the private CHAT_AGENT_HOME/models.json configuration
or supplied through CHAT_AGENT_* environment variables. Public API responses never
include key values. Keep the home directory private; protect Windows filesystem
permissions using your deployment account. Tokens are held in browser storage.

Attachments and generated files are limited to the current session. The chat
backend has no terminal, arbitrary command, project index, or team tools.
Treat uploaded documents and model-generated files as untrusted content.

Do not include credentials, personal chat history, databases or logs in public
issues. Use a private security report channel on the hosting repository once
the project is published. A maintainer contact should be set before publication.
