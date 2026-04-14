package opamp.agent.compliance.environment

violations contains msg if {
    not environment
    msg := "Agent must have an environment tag"
}

violations contains msg if {
    environment
    count(environment) == 0
    msg := "Agent environment cannot be empty"
}

environment := env if {
    attr := input.description.nonIdentifyingAttributes[_]
    attr.key == "environment"
    env := attr.value.stringValue
}
