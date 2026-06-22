import { Check, Lock } from 'lucide-react'
import { Spinner } from './ui'

// Horizontal workflow strip. Steps in the 'ready' state become buttons that
// trigger their action via onStepAction(key); every node carries a tooltip
// explaining what it is or why it's blocked.
function Pipeline({ steps, onStepAction }) {
  return (
    <div className="pipeline" aria-label="Workflow progress">
      {steps.map((step, i) => {
        const clickable = Boolean(step.action && step.state === 'ready' && onStepAction)
        const Node = clickable ? 'button' : 'div'
        return (
          <div key={step.key} className={`pipe-step pipe-${step.state}`}>
            {i > 0 && <div className="pipe-connector" />}
            <Node
              className={`pipe-node ${clickable ? 'clickable' : ''}`}
              data-tip={step.hint}
              {...(clickable ? { type: 'button', onClick: () => onStepAction(step.key) } : {})}
            >
              <span className="pipe-dot">
                {step.state === 'done' && <Check size={11} strokeWidth={3} />}
                {step.state === 'running' && <Spinner size={11} />}
                {step.state === 'soon' && <Lock size={9} />}
              </span>
              <span className="pipe-label">{step.label}</span>
            </Node>
          </div>
        )
      })}
    </div>
  )
}

export default Pipeline
