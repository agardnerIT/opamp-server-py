package opamp.agent.compliance.agent_name

violations contains msg if {
    not agent_name
    msg := "Agent must have an agent.name"
}

violations contains msg if {
    agent_name
    count(agent_name) == 0
    msg := "Agent agent.name cannot be empty"
}

agent_name := name if {
    attr := input.description.nonIdentifyingAttributes[_]
    attr.key == "agent.name"
    name := attr.value.stringValue
}
