#!/usr/bin/env groovy
/*
    Groovy script to publish the report generated by baremetal specific pipeline
    Accepts test results <MAP>, rhbuild <String>, scenarioName <String>,
    rhcephInfo <Map>
    e.g :results = ["StageName1":[suite1: [Result: "Pass", Logs:"path to log"],
                                  suite2: [Result: "PASS", Logs:"path to log"],
                                  suite6: [Result: "PASS", Logs:"path to log"]],
                    "StageName2":[suite3: [Result: "Pass", Logs:"path to log"],
                                  suite4: [Result: "Fail", Logs:"path to log"],
                                  suite5: [Result: "Fail", Logs:"path to log"]],
                    "StageName3":[suite13: [Result: "Pass", Logs:"path to log"]]]

        rhbuild  = 6.0
        scenarioName = Tier-1 Green Field
        rhcephInfo = ["repository":"registry-proxy.engineering.redhat.com/rh-osbs/rhceph:ceph-5.3-rhel-8-containers-candidate-40598-20220926221854",
                      "composes": ["rhel-8": "http://download.eng.bos.redhat.com/rhel-8/composes/auto/ceph-5.3-rhel-8/RHCEPH-5.3-RHEL-8-20220926.ci.0",
                                   "rhel-9": "http://download.eng.bos.redhat.com/rhel-9/composes/auto/ceph-5.3-rhel-9/RHCEPH-5.3-RHEL-9-20220926.ci.0"],
                      "ceph-version": "16.2.10-48"]

*/

def sendEMail(def testResults, def rhbuild, def scenarioName, def rhcephInfo) {
    /*
        Send Email notification mentioning the stages, corresponding
        suites and their execution status
    */
    def body = readFile("pipeline/vars/emailable-report.html")
    def gchatResult = ""

    body += "</table>"
    body += "<body><u><h3>Test Summary</h3></u><br />"
    body += "<p>Logs are available at ${env.BUILD_URL}</p><br />"
    body += "<table>"
    heading = "<tr><th>Stage</th><th>Suite</th><th>Result</th><th>Logs</th></tr>"
    body += heading
    def status = "PASSED"
    for ( stage in testResults ) {
        body += "<tr><td rowspan = \"${stage.value.size()}\">${stage.key}</td>"
        gchatResult+= "\nSTAGE : ${stage.key}"
        for ( suites in stage.value ) {
            res = suites.value["Result"]
            body += "<td>${suites.key}</td>"
            body += "<td>${suites.value["Result"]}</td>"
            body += "<td>${suites.value["Logs"]}</td>"
            body += "</tr>"
            gchatResult += "\n\tSuite Name : ${suites.key}"
            gchatResult += "\n\t\tResult : ${suites.value["Result"]}"
            gchatResult += "\n\t\tLogs : ${suites.value["Logs"]}"
            if (suites.value["Result"].toLowerCase() != "pass"){
                status = "FAILED"
            }
        }
        gchatResult += "\n\n"
    }
    body +="</table> </body> </html>"

    def composeUrl = ""
    for (compose in rhcephInfo["composes"]){
        composeUrl += compose.key
        composeUrl += " : " + compose.value
        composeUrl += "<br /><br />"
    }
    def cephVer = rhcephInfo["ceph-version"]
    body += "<h3><u>Test Artifacts</h2></u><table><tr><td>COMPOSE_URL </td><td>${composeUrl}</td></tr><td>CEPH_VERSION</td><td> ${cephVer}</td></tr>"
    body += "<tr><td> REPOSITORY </td><td>${rhcephInfo["repository"]}</td></tr>"

    subject = "[UPI Pipeline] [${rhbuild}] Test execution status of ${scenarioName} - ${env.BUILD_NUMBER} : ${status}"
    toList = "cephci@redhat.com"
    println subject
    println body
    emailext (
        mimeType: 'text/html',
        subject: "${subject}",
        body: "${body}",
        from: "cephci@redhat.com",
        to: "${toList}"
    )
    sendGChatNotification(gchatResult, subject)
}

def sendGChatNotification(def testResults, subject) {
    /*
        Send a GChat notification.
        Plugin used:
            googlechatnotification which allows to post build notifications to a Google
            Chat Messenger groups.
            parameter:
                url: Mandatory String parameter.
                     Single/multiple comma separated HTTP URLs or/and single/multiple
                     comma separated Credential IDs.
                message: Mandatory String parameter.
                         Notification message to be sent.
    */

    def msg= "Run result for ${subject} \n\n ${testResults}"
    googlechatnotification(url: "id:rhcephCIGChatRoom", message: msg)
}

data = args
sendEMail(data)