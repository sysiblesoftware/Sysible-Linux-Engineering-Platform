import React from 'react'
import { createRoot } from 'react-dom/client'
import { loader } from '@monaco-editor/react'
import * as monaco from 'monaco-editor'
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import jsonWorker from 'monaco-editor/esm/vs/language/json/json.worker?worker'
import App from './App.jsx'
import './styles.css'

// Self-host Monaco (no runtime CDN — works airgapped). The full monaco-editor
// import registers all basic languages, including yaml and hcl (Terraform).
self.MonacoEnvironment = {
  getWorker(_id, label) {
    if (label === 'json') return new jsonWorker()
    return new editorWorker()
  },
}
loader.config({ monaco })

createRoot(document.getElementById('root')).render(<App />)
