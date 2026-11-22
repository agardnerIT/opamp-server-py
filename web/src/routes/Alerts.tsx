import StubPage from "./StubPage";

export default function Alerts() {
  return (
    <StubPage
      title="Alerts"
      stage={8}
      description="Webhook alert configuration: which fleet events notify where, with a delivery test button. Webhook bodies follow the same redaction round-trip rules as config bodies — a <redacted> sentinel means keep the stored value."
    />
  );
}
