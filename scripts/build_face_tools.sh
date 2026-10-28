#!/bin/zsh
# Build the on-device face finder (Apple Vision). Needs Xcode command line tools.
cd "${0:A:h}/../face" && swiftc -O facefind.swift -o facefind && echo "built face/facefind"
