package opamp.agent.compliance.description

violations contains msg if {
    not description
    msg := "Agent must have a description"
}

violations contains msg if {
    description
    count(description) == 0
    msg := "Agent description cannot be empty"
}

description := desc if {
    attr := input.description.nonIdentifyingAttributes[_]
    attr.key == "description"
    desc := attr.value.stringValue
}
