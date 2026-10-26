#!/usr/bin/env node
import { main } from '../npm/cli.mjs'

process.exitCode = await main()
