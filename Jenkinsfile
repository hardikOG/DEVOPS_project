// CI/CD for the self-healing platform.
//
// test -> build images -> load into cluster -> deploy -> smoke test -> chaos gate
//
// The chaos gate is the interesting part: every build deliberately breaks the
// freshly deployed system and only passes if the healer recovers it in time.
pipeline {
  agent any

  options {
    timestamps()
    disableConcurrentBuilds()
    timeout(time: 30, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '20'))
  }

  parameters {
    string(name: 'CLUSTER_RUNTIME', defaultValue: env.CLUSTER_RUNTIME ?: 'k3s-docker',
           description: 'k3s-docker | shared-docker - how built images reach the cluster (scripts/load-images.sh)')
    booleanParam(name: 'RUN_CHAOS_GATE', defaultValue: true, description: 'Break the system and require auto-recovery')
  }

  environment {
    IMAGE_TAG   = "1.0.${env.BUILD_NUMBER}"
    KUBECONFIG  = "${env.KUBECONFIG ?: '/var/jenkins_home/.kube/config'}"
    HEALER_URL  = "${env.HEALER_URL ?: 'http://host.docker.internal:30080'}"
    PIP_DISABLE_PIP_VERSION_CHECK = '1'
  }

  stages {
    stage('Checkout') {
      steps {
        checkout scm
        sh 'git log -1 --oneline'
      }
    }

    stage('Lint & unit tests') {
      steps {
        sh '''
          python3 -m venv .ci-venv
          . .ci-venv/bin/activate
          pip install -q -r requirements-dev.txt
          ruff check healer services tests
          pytest -q --junitxml=reports/junit.xml --cov=healer/healer --cov-report=xml:reports/coverage.xml
        '''
      }
      post { always { junit allowEmptyResults: true, testResults: 'reports/junit.xml' } }
    }

    stage('Validate manifests') {
      steps {
        sh 'kubectl kustomize k8s > reports/rendered.yaml && grep -c "^kind:" reports/rendered.yaml'
      }
    }

    stage('Build images') {
      parallel {
        stage('demo-service') {
          steps { sh 'docker build -t self-healing/demo-service:${IMAGE_TAG} -t self-healing/demo-service:1.0.0 services/demo-service' }
        }
        stage('healer') {
          steps { sh 'docker build -t self-healing/healer:${IMAGE_TAG} -t self-healing/healer:1.0.0 healer' }
        }
      }
    }

    stage('Ship images to cluster') {
      steps { sh 'CLUSTER_RUNTIME=${CLUSTER_RUNTIME} IMAGE_TAG=${IMAGE_TAG} scripts/load-images.sh' }
    }

    stage('Deploy') {
      steps { sh 'IMAGE_TAG=${IMAGE_TAG} scripts/deploy.sh' }
    }

    stage('Smoke test') {
      steps {
        sh '''
          for i in $(seq 1 30); do
            curl -fsS "$HEALER_URL/healthz" && break
            sleep 2
          done
          curl -fsS "$HEALER_URL/api/state" | python3 -c "import json,sys; s=json.load(sys.stdin); bad=[v['name'] for v in s['services'] if v['status']!='healthy']; print('services:', [v['name'] for v in s['services']]); sys.exit(1 if bad or not s['services'] else 0)"
        '''
      }
    }

    stage('Chaos gate') {
      when { expression { params.RUN_CHAOS_GATE } }
      steps {
        sh 'python3 scripts/chaos_test.py --url "$HEALER_URL" --scenarios kill_pod,crash,errors,outage --timeout 150 --report reports/chaos-report.json'
      }
    }
  }

  post {
    always {
      archiveArtifacts allowEmptyArchive: true, artifacts: 'reports/**'
    }
    failure {
      sh 'kubectl get pods -A -o wide || true'
      sh 'kubectl -n self-healing logs deploy/healer --tail=200 || true'
    }
  }
}
