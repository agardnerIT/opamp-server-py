import StubPage from "./StubPage";

export default function AgentTokens() {
  return (
    <StubPage
      title="Agent tokens"
      stage={8}
      description="Mint and revoke per-agent authentication tokens. Requires the admin role; the server enforces it either way."
    />
  );
}
