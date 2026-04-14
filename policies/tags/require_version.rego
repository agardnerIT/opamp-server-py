package opamp.agent.compliance.version

violations contains msg if {
    not version
    msg := "Agent must have a version"
}

violations contains msg if {
    version
    count(version) == 0
    msg := "Agent version cannot be empty"
}

version := ver if {
    attr := input.description.identifyingAttributes[_]
    attr.key == "service.version"
    ver := attr.value.stringValue
}
